from collections import deque, defaultdict
from contextlib import suppress
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterator, Deque, Type, TypeVar, cast, Union
import asyncio
import copy
import datetime
import logging
import re
import subprocess
import time

from __main__ import send_cmd_help, settings
from cogs.utils import checks
from cogs.utils.chat_formatting import pagify, escape
from cogs.utils.dataIO import dataIO
from discord.ext import commands
import discord

__author__ = "tikki"
__version__ = "1.0.0"


module_id = 'fahrstuhl'
log = logging.getLogger(f'red.{module_id}')

ServerId = str
ChannelId = str
AudioPlayer = Any # discord.ProcessPlayer

class Muzak:
    """Endless horror"""

    def __init__(self, bot: commands.Bot, player: str) -> None:
        self.bot = bot
        self.queues: Dict[ServerId, Queue] = defaultdict(Queue)
        self.play_logs: Dict[ServerId, deque] = defaultdict(deque)
        self.settings = load_settings()
        self.server_specific_setting_keys = 'volume',
        self.local_tracks_path = data_path() / 'tracks'
        self.players: Dict[ServerId, AudioPlayer] = {}
        self.playlists: Dict[ServerId, Playlist] = {}

        self.skip_votes: Dict[ServerId, List] = {}

        self.connect_timers: Dict[ServerId, int] = {}

        self.settings['use_avconv'] = player == 'avconv'
        self.save_settings()

        bot.loop.create_task(self.queue_scheduler())
        bot.loop.create_task(self.local_tracks_autoupdater())
        bot.loop.create_task(self.cleanup_on_unload())

    def save_settings(self) -> None:
        save_settings(self.settings)

    @commands.command(name="fahrstuhl", hidden=True, pass_context=True, no_pm=True)
    @checks.is_owner()
    async def command_play(self, ctx: commands.Context) -> None:
        """Plays a local tracks"""
        server: Optional[discord.Server] = ctx.message.server
        author: discord.User = ctx.message.author
        channel: Union[discord.Channel,discord.PrivateChannel] = ctx.message.channel

        if server is None: # i.e. channel.is_private
            await self.say('You need to say this in a channel.')
            return

        if not author.voice_channel:
            await self.say('You need to be in a voice channel.')
            return

        if self.is_playing(server, author.voice_channel):
            await self.say("Turn up your volume, I'm already playing!")
            return

        try:
            await self.join_voice_channel(author.voice_channel)
        except UnauthorizedConnect:
            await self.say("I'm not allowed to join your voice channel.")
            return
        except UnauthorizedSpeak:
            await self.say("I'm not allowed to speak in your voice channel.")
            return
        except ChannelUserLimit:
            await self.say('Your voice channel is full.')
            return

        self.play_local_tracks(server)

    async def say(self, what: str) -> None:
        await self.bot.say(what)

    def check_can_join(self, channel: discord.Channel) -> None:
        if channel.is_private:
            raise PrivateChannel

        server = channel.server
        if server is None:
            raise NotOnServer

        if channel.type is not discord.ChannelType.voice:
            raise NotAVoiceChannel

        perms = channel.permissions_for(server.me)

        if perms.connect is False:
            raise UnauthorizedConnect

        if perms.speak is False:
            raise UnauthorizedSpeak

        is_admin = perms.administrator
        is_full = channel.user_limit != 0 and len(channel.voice_members) >= channel.user_limit
        if is_full and not is_admin:
            raise ChannelUserLimit

    def play_local_tracks(self, server: discord.Server) -> None:
        playlist_name = 'local-tracks'
        if self.queues[server.id].playlist_name() == playlist_name:
            return
        playlist = Playlist(name=playlist_name,
                            files=self.local_tracks(), repeat=True)
        self.playlists[playlist_name] = playlist
        self.queue_playlist(server, playlist)

    def local_tracks(self) -> Iterator[Path]:
        return self.local_tracks_path.iterdir()

    def queue_playlist(self, server: discord.Server, playlist: 'Playlist') -> None:
        log.debug(f'setting up playlist {playlist.name} on sid {server.id}')

        self.stop_player(server)

        log.debug(f'finished resetting state on sid {server.id}')

        self.queues[server.id] = Queue.from_playlist(playlist)

    def stop_player(self, server: discord.Server) -> None:
        player = self.audio_player(server)
        if player:
            player.stop()

    def voice_client(self, server: discord.Server) -> Optional[discord.VoiceClient]:
        return self.bot.voice_client_in(server)

    def audio_player(self, server: discord.Server) -> Optional[AudioPlayer]:
        return self.players.get(server.id)

    def is_voice_connected(self, server: discord.Server) -> bool:
        return self.bot.is_voice_connected(server)

    def is_playing(self, server: discord.Server, channel: discord.Channel=None) -> bool:
        player = self.audio_player(server)
        is_playing_on_server = player is not None and not player.is_done()
        if channel is None:
            return is_playing_on_server
        return is_playing_on_server and channel == server.me.voice_channel

    def server_settings(self, server: discord.Server) -> Dict[str, Any]:
        if server.id not in self.settings['servers']:
            self.settings['servers'][server.id] = {}
        settings = self.settings['servers'][server.id]

        for key in self.server_specific_setting_keys:
            if key not in settings:
                settings[key] = self.settings[key]
        # self.save_settings()

        return settings

    def server(self, server_id: ServerId) -> Optional[discord.Server]:
        return self.bot.get_server(server_id)

    def joined_voice_channel(self, server: discord.Server) -> Optional[discord.Channel]:
        return server.me.voice_channel
        # voice_client = self.voice_client(server)
        # if voice_client is not None:
        #     return voice_client.channel

    async def play(self, server: discord.Server, song: 'Song') -> None:
        """Returns the song object of what's playing"""
        assert type(server) is discord.Server
        log.debug(f'starting to play on "{server.name}"')

        if not song.path or not song.is_playable():
            raise FileNotFoundError

        player = await self.create_audio_player(server, song.path,
                                                start_time=song.start_time,
                                                end_time=song.end_time)
        player.start()
        play_start_time = datetime.datetime.now()
        self.play_logs[server.id].append((play_start_time, song))
        log.debug(f'starting player on sid {server.id}')

    async def create_audio_player(self, server: discord.Server, filepath: Path,
                                  start_time: str=None, end_time: str=None) -> AudioPlayer:
        """This function will guarantee we have a valid voice client,
            even if one doesn't exist previously."""
        voice_client = self.voice_client(server)
        voice_channel_id = self.queues[server.id].voice_channel_id

        if voice_client is None:
            log.debug(f'not connected when we should be in {server.id}#{voice_channel_id}')
            channel = self.bot.get_channel(voice_channel_id)
            if channel is None:
                raise VoiceNotConnected('Channel does not exist.')
            log.debug(f'reconnecting to {server.id}#{voice_channel_id} ...')
            voice_client = await self.join_voice_channel(channel)

        if voice_client.channel.id != voice_channel_id:
            self.queues[server.id].voice_channel_id = voice_client.channel.id
            log.debug(f'reconnect chan id for sid {server.id} is wrong, fixing')

        options = '-b:a 64k -bufsize 64k'
        before_options = ''

        if start_time:
            before_options += f'-ss {start_time}'
        if end_time:
            options += f' -to {end_time} -copyts'

        previous_player = self.audio_player(server)
        if previous_player is not None:
            try:
                previous_player.process.kill()
            except ProcessLookupError:
                log.warning('could not kill old player')
            else:
                log.debug('killed old player')

        log.debug(f'making player on sid {server.id}')

        player: AudioPlayer = voice_client.create_ffmpeg_player(
            filepath.resolve().as_posix(),
            use_avconv=self.settings['use_avconv'],
            options=options, before_options=before_options)
        self.players[server.id] = player

        player.volume = self.server_settings(server)['volume'] / 100

        return player

    async def join_voice_channel(self, channel: discord.Channel, switch: bool=True) -> discord.VoiceClient:
        server = channel.server
        if channel == self.joined_voice_channel(server):
            log.info(f'already joined {server.id}#{channel.id}')
            return # already connected to this channel

        self.check_can_join(channel)

        if switch:
            voice_client = self.voice_client(server)
            if voice_client is not None:
                await voice_client.move_to(channel)
                return voice_client

        connect_time = self.connect_timers.get(server.id, 0)
        if time.time() < connect_time:
            diff = int(connect_time - time.time())
            raise ConnectTimeout(f'You are on connect cooldown for another {diff} seconds.')
        if server.id in self.queues:
            self.queues[server.id].voice_channel_id = channel.id
        try:
            await asyncio.wait_for(self.bot.join_voice_channel(channel),
                                   timeout=30, loop=self.bot.loop)
        except asyncio.futures.TimeoutError as e:
            log.exception(str(e))
            self.connect_timers[server.id] = int(time.time()) + 300
            raise ConnectTimeout("We timed out connecting to a voice channel,"
                                 " please try again in 10 minutes.")
        return self.voice_client(server)

    async def on_voice_state_update(self, before: discord.Member, after: discord.Member) -> None:
        server = after.server
        # Member objects
        if after.voice_channel != before.voice_channel:
            with suppress(ValueError, KeyError):
                self.skip_votes[server.id].remove(after.id)
        if after is None:
            return
        if server.id not in self.queues:
            return
        if after != server.me:
            return

        # Member is the bot

        if before.voice_channel != after.voice_channel:
            self.queues[after.server.id].voice_channel_id = after.voice_channel.id

        if before.mute != after.mute:
            player = self.audio_player(server)
            if not player:
                return
            if after.mute and player.is_playing():
                log.debug("Just got muted, pausing")
                player.pause()
            elif not after.mute and \
                    (not player.is_playing() and
                     not player.is_done()):
                log.debug("just got unmuted, resuming")
                player.resume()

    async def play_next_song_in_queue(self, server_id: ServerId) -> None:
        server = self.server(server_id)

        if not server:
            return

        if self.is_playing(server):
            return

        queue = self.queues[server.id]

        should_reload = queue.playlist and queue.playlist.repeat
        if queue.is_empty() and should_reload:
            queue.reload_from_playlist()

        song = queue.next_song()
        if song is None:
            log.debug('No more songs to play.')
            return

        log.debug(f'not playing anything on sid {server.id},'
                   ' attempting to start a new song.')
        self.skip_votes[server.id] = []
        # Reset skip votes for each new song

        await self.play(server, song)
        self.queues[server.id].now_playing = song
        log.debug(f'set now_playing for sid {server.id}')

    def is_loaded(self) -> bool:
        return self is self.bot.get_cog('Muzak')

    async def local_tracks_autoupdater(self) -> None:
        # we're lacking a good lib to watch for fs changes, so we'll have to do it ourselves
        while self.is_loaded():
            if 'local-tracks' in self.playlists:
                playlist = self.playlists['local-tracks']
                playlist.files = list(self.local_tracks())
            await asyncio.sleep(30)

    async def queue_scheduler(self) -> None:
        while self.is_loaded():
            tasks = []
            queues = copy.deepcopy(self.queues)
            for server_id in queues:
                tasks.append(
                    self.bot.loop.create_task(self.play_next_song_in_queue(server_id)))
            while not all(t.done() for t in tasks):
                await asyncio.sleep(0.5)
            await asyncio.sleep(1)

    async def cleanup_on_unload(self) -> None:
        while self.is_loaded():
            await asyncio.sleep(0.5)

        for player in self.players.values():
            with suppress(BaseException):
                player.stop()

    def __unload(self) -> None:
        for vc in self.bot.voice_clients:
            self.bot.loop.create_task(vc.disconnect())


def setup(bot: commands.Bot) -> None:
    check_folders()
    check_files()
    load_opus()

    bot.add_cog(Muzak(bot, player()))


def is_command(command: str) -> bool:
    try:
        # subprocess.run(['command', '-v', command],
        #                check=True, stdout=subprocess.DEVNULL)
        subprocess.run([command, '-version'],
                       check=True, stdout=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


known_players = 'ffmpeg', 'avconv'


def player() -> str:
    for player in known_players:
        if is_command(player):
            return player
    raise RuntimeError('No player found.')


def load_opus() -> None:
    try:
        if not discord.opus.is_loaded():
            discord.opus.load_opus('libopus-0.dll')
    except OSError:  # Incorrect bitness
        raise RuntimeError(
            "Your opus library's bitness must match your python installation's"
            " bitness. They both must be either 32bit or 64bit.")
    except:
        raise RuntimeError(
            "You need to install ffmpeg and opus. See \"https://github.com/"
            "Twentysix26/Red-DiscordBot/wiki/Requirements\"")


def data_path() -> Path:
    return Path('data') / module_id


def check_folders() -> None:
    folders = data_path(),
    for folder in folders:
        if not folder.exists():
            log.info(f'Creating folder: {folder}')
            folder.mkdir(parents=True)


def default_config() -> dict:
    return {'volume': 100, 'servers': {}, 'vote_threshold': 50}


def load_settings() -> dict:
    settings_path = data_path() / 'settings.json'
    settings = dataIO.load_json(settings_path)
    return settings if isinstance(settings, dict) else {}


def save_settings(settings: dict) -> None:
    settings_path = data_path() / 'settings.json'
    dataIO.save_json(settings_path, settings)


def check_files() -> None:
    default = default_config()

    try:
        current = load_settings()
    except (FileNotFoundError, JSONDecodeError):
        log.info('Creating default settings...')
        save_settings(default)
        return

    # consistency check
    if current.keys() != default.keys():
        for key in default.keys():
            if key not in current.keys():
                current[key] = default[key]
                log.info(f'Adding {key} field to settings')
        save_settings(current)


class NotConnected(Exception):
    pass


class VoiceNotConnected(NotConnected):
    pass


class PrivateChannel(Exception):
    pass


class NotOnServer(Exception):
    pass


class NotAVoiceChannel(Exception):
    pass


class UnauthorizedConnect(Exception):
    pass


class UnauthorizedSpeak(Exception):
    pass


class ChannelUserLimit(Exception):
    pass


class ConnectTimeout(NotConnected):
    pass

QueueType = TypeVar('QueueType', bound='Queue')
class Queue:
    def __init__(self, playlist: 'Playlist'=None, voice_channel_id: ChannelId=None,
                       songs: Deque['Song']=None, now_playing: 'Song'=None) -> None:
        self.playlist = playlist
        self.voice_channel_id = voice_channel_id
        self.songs = songs or deque()
        self.now_playing = now_playing

    def clear(self) -> None:
        self.songs.clear()

    def next_song(self) -> Optional['Song']:
        while len(self.songs) > 0:
            song = self.songs.popleft()
            if song.is_playable():
                return song
        return None

    def reload_from_playlist(self) -> None:
        if not self.playlist:
            return
        self.songs = deque(Song.from_path(path) for path in self.playlist.available_files())

    def playlist_name(self) -> Optional[str]:
        return self.playlist.name if self.playlist else None

    def is_empty(self) -> bool:
        return len(self.songs) == 0

    @classmethod
    def from_playlist(Class: Type[QueueType], playlist: 'Playlist') -> QueueType:
        q = Class(playlist=playlist)
        q.reload_from_playlist()
        return q

SongType = TypeVar('SongType', bound='Song')
class Song:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__ = kwargs
        self.title = kwargs.pop('title', None)
        self.url = cast(Optional[str], kwargs.pop('url', None))
        self.path = cast(Optional[Path], kwargs.pop('path', None))
        self.webpage_url = kwargs.pop('webpage_url', "")
        self.duration = cast(int, kwargs.pop('duration', 0))
        self.start_time = kwargs.pop('start_time', None)
        self.end_time = kwargs.pop('end_time', None)

    @property
    def id(self) -> str:
        if self.url:
            return self.url
        if self.path:
            return self.path.resolve().as_uri()
        return ''

    def duration_delta(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.duration)

    def is_playable(self) -> bool:
        return self.path is not None and self.path.is_file()

    @classmethod
    def from_path(Class: Type[SongType], path: Path) -> SongType:
        return Class(title=path.stem, path=path)

class Playlist:
    def __init__(self, server: discord.Server=None, name: str=None,
                 author: str=None, url: str=None, files: Iterator[Path]=None,
                 path: Path=None, repeat: bool=False) -> None:
        self.server = server
        self.name = name
        self.author = author
        self.url = url
        self.files: List[Path] = list(files) if files else []
        self.path = path
        self.repeat: bool = repeat

    def available_files(self) -> Iterator[Path]:
        return (path for path in self.files if path.is_file())

    def to_json(self) -> Dict[str, Any]:
        return {'author': self.author, 'files': self.files,
                'url': self.url, 'name': self.name}

    def is_author(self, user: discord.User) -> bool:
        return user.id == self.author

    def save(self) -> None:
        dataIO.save_json(self.path, self.to_json())
