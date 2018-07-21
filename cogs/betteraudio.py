from collections import deque, defaultdict
from contextlib import suppress
from json import JSONDecodeError
from pathlib import Path
from typing import *
import asyncio
import copy
import datetime
import logging
import random
import re
import subprocess
import time
import uuid

from cogs.utils import checks
from cogs.utils.chat_formatting import pagify, escape
from cogs.utils.dataIO import dataIO
from discord.ext import commands
import discord
import mutagen

__author__ = "tikki"
__version__ = "1.0.0"


module_id = 'betteraudio'
log = logging.getLogger(f'red.{module_id}')

SongId = str
ServerId = str
ChannelId = str
AudioPlayer = Any  # discord.ProcessPlayer


MuzakCoro = Callable[['Muzak'], Coroutine[None, None, None]]


_while_loaded_tasks = []
def while_loaded(func: MuzakCoro) -> MuzakCoro:
    global _while_loaded_tasks
    async def wrapper(self: 'Muzak') -> None:
        while self.is_loaded():
            await func(self)
            await asyncio.sleep(1)
    _while_loaded_tasks.append(wrapper)
    return wrapper


class Muzak:
    """Endless horror"""

    def __init__(self, bot: commands.Bot, player: str) -> None:
        self.bot = bot
        self.queues: Dict[ServerId, Queue] = defaultdict(Queue)
        # self.play_logs: Dict[ServerId, deque] = defaultdict(deque)
        self.settings = load_settings()
        self.server_specific_setting_keys = 'volume',
        self.players: Dict[ServerId, AudioPlayer] = {}
        self.playlists: Dict[ServerId, BasePlaylist] = {}

        self.no_listeners_timeouts: Dict[ServerId, float] = {}

        # self.skip_votes: Dict[ServerId, List] = {}

        self.connect_timers: Dict[ServerId, float] = defaultdict(time.time)

        self.settings['use_avconv'] = player == 'avconv'

        for task in _while_loaded_tasks:
            bot.loop.create_task(task(self))

    def save_settings(self) -> None:
        save_settings(self.settings)

    @no_type_check
    @commands.group(pass_context=True)
    async def zak(self, ctx):
        """Play some muzak"""
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)
            return

    @no_type_check
    @zak.command(name="play", pass_context=True, no_pm=True)
    @checks.is_owner()
    async def command_play(self, ctx: commands.Context, playlist_name: str) -> None:
        """Play a playlist"""
        server: Optional[discord.Server] = ctx.message.server
        author: discord.User = ctx.message.author
        channel: Union[discord.Channel, discord.PrivateChannel] = ctx.message.channel

        if server is None:  # i.e. channel.is_private
            await self.say('You need to say this in a channel.')
            return

        if not author.voice_channel:
            await self.say('You need to be in a voice channel.')
            return

        if (self.is_playing(server, author.voice_channel) and
                server.id in self.playlists and
                self.playlists[server.id].name == playlist_name):
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

        self.play_playlist(server, playlist_name)

    @no_type_check
    @zak.command(name="now?", pass_context=True, no_pm=True)
    async def command_whats_playing(self, ctx: commands.Context) -> None:
        """Show what's playing"""
        server: Optional[discord.Server] = ctx.message.server
        author: discord.User = ctx.message.author
        channel: Union[discord.Channel, discord.PrivateChannel] = ctx.message.channel

        if server.id not in self.queues:
            await self.say("I can't remember what I last played, sorry.")
            return

        now_playing = self.queues[server.id].now_playing
        if not now_playing:
            await self.say("I'm not playing anything at the moment.")
            return

        await self.say(now_playing.metadata())

    @no_type_check
    @zak.group(name="next", pass_context=True, no_pm=True)
    @checks.is_owner()
    async def command_play_next(self, ctx: commands.Context) -> None:
        """Skip a song or group"""
        if self.command_play_next is ctx.invoked_subcommand:
            await self.bot.send_cmd_help(ctx)
            return

    @no_type_check
    @command_play_next.command(name="song", pass_context=True, no_pm=True)
    async def command_play_next_song(self, ctx: commands.Context) -> None:
        server: Optional[discord.Server] = ctx.message.server
        author: discord.User = ctx.message.author
        channel: Union[discord.Channel, discord.PrivateChannel] = ctx.message.channel

        if (server.id not in self.queues or
                self.queues[server.id].now_playing is None):
            await self.say("I'm not playing anything at the moment.")
            return

        self.skip_current_song(server)

    @no_type_check
    @command_play_next.command(name="group", pass_context=True, no_pm=True)
    async def command_play_next_group(self, ctx: commands.Context) -> None:
        server: Optional[discord.Server] = ctx.message.server
        author: discord.User = ctx.message.author
        channel: Union[discord.Channel, discord.PrivateChannel] = ctx.message.channel

        if (server.id not in self.queues or
                self.queues[server.id].now_playing is None):
            await self.say("I'm not playing anything at the moment.")
            return

        self.skip_current_group(server)

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
        is_full = channel.user_limit != 0 and len(
            channel.voice_members) >= channel.user_limit
        if is_full and not is_admin:
            raise ChannelUserLimit

    def play_playlist(self, server: discord.Server, playlist_name: str) -> None:
        if self.queues[server.id].playlist_name() == playlist_name:
            return
        log.debug(f'loading playlist {playlist_name} '
                  f'into queue on server {server.name}')
        playlist = FancyPlaylist.with_name(playlist_name)
        self.queues[server.id] = Queue.from_playlist(playlist)
        self.playlists[server.id] = playlist
        self.stop_player(server)

    def stop_player(self, server: discord.Server) -> None:
        player = self.audio_player(server)
        if player:
            log.debug(f'stopping player on server {server.name}')
            player.stop()

    def voice_client(self, server: discord.Server) -> Optional[discord.VoiceClient]:
        return self.bot.voice_client_in(server)

    def audio_player(self, server: discord.Server) -> Optional[AudioPlayer]:
        return self.players.get(server.id)

    def is_voice_connected(self, server: discord.Server) -> bool:
        return self.bot.is_voice_connected(server)

    def is_playing(self, server: discord.Server,
                         channel: discord.Channel=None) -> bool:
        '''Return if we should be playing at the moment.

        Even if this function returns true, that does not mean we are actually
        sending audio out into the world! A player can be paused (e.g. because
        the bot was muted or all listeners left the channel) and this will still
        return true.
        '''
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

        return settings

    def server(self, server_id: ServerId) -> Optional[discord.Server]:
        return self.bot.get_server(server_id)

    def joined_voice_channel(self, server: discord.Server) -> Optional[discord.Channel]:
        return server.me.voice_channel
        # voice_client = self.voice_client(server)
        # if voice_client is not None:
        #     return voice_client.channel

    async def play(self, server: discord.Server, song: 'Song') -> None:
        if not song.is_playable():
            log.debug(f'song is not playable: {song}')
            raise SongNotPlayable

        player = await self.create_audio_player(server, song.path,
                                                start_time=song.start_time,
                                                end_time=song.end_time)
        player.start()
        # play_start_time = datetime.datetime.now()
        # self.play_logs[server.id].append((play_start_time, song))
        log.debug(f'playing on server {server.name}: {song} '
                  f'({song.metadata()})')

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
            log.debug(f'already joined {server.name}#{channel.id}')
            return  # already connected to this channel

        self.check_can_join(channel)

        if switch:
            voice_client = self.voice_client(server)
            if voice_client is not None:
                await voice_client.move_to(channel)
                return voice_client

        connect_time = self.connect_timers[server.id]
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
            self.connect_timers[server.id] = time.time() + 300
            raise ConnectTimeout("We timed out connecting to a voice channel,"
                                 " please try again in 10 minutes.")
        return self.voice_client(server)

    def is_alone(self, server: discord.Server) -> bool:
        '''Return wether the bot is alone in a voice channel'''
        channel = self.joined_voice_channel(server)
        return channel and len(channel.voice_members) == 1

    async def on_voice_state_update(self, before: Optional[discord.Member],
                                    after: Optional[discord.Member]) -> None:
        '''Called whenever any user's voice state changes, not only our own.'''
        if before is None or after is None:
            return

        server = after.server

        if server is None:
            return

        is_self = after == server.me

        # if after.voice_channel != before.voice_channel:
        #     with suppress(ValueError, KeyError):
        #         self.skip_votes[server.id].remove(after.id)
        if server.id not in self.queues:
            return

        player = self.audio_player(server)
        is_resumable = (not server.me.mute and
                        not player.is_playing() and not player.is_done())

        if player:
            is_bot_alone = self.is_alone(server)
            if is_bot_alone and player.is_playing():
                log.debug('All listeners left, pausing')
                player.pause()
                self.no_listeners_timeouts[server.id] = time.time()
            elif not is_bot_alone and is_resumable:
                log.debug('Listeners available, resuming')
                player.resume()
                del self.no_listeners_timeouts[server.id]

        if is_self and before.voice_channel != after.voice_channel:
            self.queues[server.id].voice_channel_id = after.voice_channel.id

        if is_self and before.mute != after.mute:
            if after.mute and player.is_playing():
                log.debug('Just got muted, pausing')
                player.pause()
            elif is_resumable:
                log.debug('just got unmuted, resuming')
                player.resume()

    async def play_next_song_in_queue(self, server_id: ServerId) -> None:
        server = self.server(server_id)

        if not server:
            log.debug(f'could not find server for sid {server_id}')
            return

        if self.is_playing(server):
            return

        if self.is_alone(server):
            return

        log.debug(f'not playing anything on server {server.name},'
                   ' attempting to start a new song.')

        queue = self.queues[server.id]

        if queue.is_empty():
            log.debug(f'refreshing queue from attached playlist')
            queue.refresh()

        song = queue.next_song()
        if song is None:
            log.debug('No more songs to play.')
            return

        await self.play(server, song)
        self.queues[server.id].now_playing = song
        # self.skip_votes[server.id] = []

    def skip_current_song(self, server: discord.Server) -> None:
        self.stop_player(server)

    def skip_current_group(self, server: discord.Server) -> None:
        if server.id in self.queues:
            self.queues[server.id].clear()
        self.stop_player(server)

    def is_loaded(self) -> bool:
        return self is self.bot.get_cog(self.__class__.__name__)

    @while_loaded
    async def clear_queue_on_pause_timeout(self) -> None:
        for server in (self.server(server_id) for server_id in self.queues):
            if not server or server.id not in self.no_listeners_timeouts:
                continue
            spent = time.time() - self.no_listeners_timeouts[server.id]
            if spent > self.settings['skip_group_timeout']:
                log.debug('no listeners timeout: '
                          f'clearing queue on server {server.name}')
                self.skip_current_group(server)
                del self.no_listeners_timeouts[server.id]

    @while_loaded
    async def queue_scheduler(self) -> None:
        # queues = copy.deepcopy(self.queues)
        task = self.bot.loop.create_task
        tasks = (task(self.play_next_song_in_queue(server_id))
                 for server_id in self.queues)
        while not all(t.done() for t in tasks):
            await asyncio.sleep(0.5)

    def __unload(self) -> None:
        for vc in self.bot.voice_clients:
            self.bot.loop.create_task(vc.disconnect())
        for player in self.players.values():
            with suppress(BaseException):
                player.stop()
        self.save_settings()


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
    return {'volume': 100, 'servers': {}, 'vote_threshold': 50,
            'skip_group_timeout': 30}


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


class SongNotPlayable(Exception):
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


class Queue:

    def __init__(self, playlist: 'BasePlaylist'=None, voice_channel_id: ChannelId=None,
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

    def refresh(self) -> None:
        if not self.playlist:
            return
        self.songs = deque(self.playlist.next_songs())

    def playlist_name(self) -> Optional[str]:
        return self.playlist.name if self.playlist else None

    def is_empty(self) -> bool:
        return len(self.songs) == 0

    @classmethod
    def from_playlist(Class, playlist: 'BasePlaylist') -> 'Queue':
        q = Class(playlist=playlist)
        q.refresh()
        return q


class Metadata:
    album: str
    albumartist: str
    artist: str
    title: str
    tracknumber: str
    tracktotal: str
    year: str
    __slots__ = ('album', 'albumartist', 'artist', 'title',
                 'tracknumber', 'tracktotal', 'year')

    @classmethod
    def from_path(Class, path: Path) -> 'Metadata':
        self = Class()
        tags = mutagen.File(path).tags
        if isinstance(tags, mutagen.id3.ID3):
            self._with_mp3_tags(tags)
        else:
            self._with_other_tags(tags)
        return self

    def _with_other_tags(self, tags: mutagen.Tags) -> None:
        for name in self.__slots__:
            values = tags.get(name, [''])
            setattr(self, name, values[0])
        if 'date' in tags:
            self.year = tags['date'][0]

    def _with_mp3_tags(self, tags: mutagen.id3.ID3) -> None:
        attrmap = {
            'album': 'TALB',
            'artist': 'TPE1',
            'title': 'TIT2',
            # 'tracknumber': 'TRCK',
            'year': 'TYER',
            # 'year': 'TDRC',
        }
        for name in self.__slots__:
            tag = tags.get(attrmap.get(name), '')
            setattr(self, name, str(tag))
        if not self.year and 'TDRC' in tags:
            self.year = str(tags['TDRC'])
        if 'TRCK' in tags:
            tag = str(tags['TRCK'])
            if '/' in tag:
                self.tracknumber, self.tracktotal = tag.split('/', 1)
            else:
                self.tracknumber = tag

    def __str__(self) -> str:
        s = f'"{self.artist or "{anonymous}"}" — "{self.title or "{untitled}"}"'
        if self.album:
            s += f' ({self.album})'
        if self.year:
            s += f' ({self.year})'
        return s

    def __repr__(self) -> str:
        return f'Metadata({ {k: getattr(self, k) for k in self.__slots__} })'


class Song:

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__ = kwargs
        self._id: SongId = kwargs.pop('id', None)
        self._meta: Metadata = None
        self.title = kwargs.pop('title', None)
        self.url = cast(Optional[str], kwargs.pop('url', None))
        self.path = cast(Optional[Path], kwargs.pop('path', None))
        self.webpage_url = kwargs.pop('webpage_url', "")
        self.duration = cast(int, kwargs.pop('duration', 0))
        self.start_time = kwargs.pop('start_time', None)
        self.end_time = kwargs.pop('end_time', None)

    @property
    def id(self) -> SongId:
        if not self._id:
            self._id = self.url or (self.path.resolve().as_uri()
                                    if self.path else uuid.uuid4().hex)
        return self._id

    def duration_delta(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.duration)

    def is_playable(self) -> bool:
        return self.path is not None and self.path.is_file()

    @classmethod
    def from_path(Class, path: Path) -> 'Song':
        return Class(title=path.stem, path=path)

    def metadata(self) -> Metadata:
        if not self._meta:
            self._meta = Metadata.from_path(self.path)
        return self._meta

    def __repr__(self) -> str:
        return str(self.path)
        return f'Song({ dict(path=self.path) })'


class BasePlaylist:
    def __init__(self, name: str=None) -> None:
        self.name = name

    def next_songs(self) -> Iterable[Song]:
        raise NotImplementedError


class Playlist(BasePlaylist):

    def __init__(self, name: str=None, files: Iterable[Path]=None,
                 path: Path=None, repeat: bool=False, shuffle: bool=False) -> None:
        super().__init__(name=name)
        self.files = files
        self.path = path
        self.repeat = repeat
        self.shuffle = shuffle

    def next_songs(self) -> Iterable[Song]:
        if self.files is None:
            return []
        return (Song.from_path(path) for path in self.files if path.is_file())

    def settings(self) -> Dict[str, Any]:
        return {'files': self.files, 'name': self.name,
                'repeat': self.repeat, 'shuffle': self.shuffle}

    def save(self) -> None:
        if not self.path:
            raise RuntimeError('Playlist has no path.')
        dataIO.save_json(self.path, self.settings())


class FancyPlaylist(BasePlaylist):

    def __init__(self, groups_dir: str, group_order: str=None, group_glob: str='*',
                 group_pattern: str='', song_order: str=None, song_glob: str='*',
                 song_pattern: str='', name: str=None, repeat: bool=False) -> None:
        super().__init__(name=name)
        self._groups: Iterable[Path] = None
        self.group_order = group_order
        self.group_glob = group_glob
        self.group_pattern = re.compile(group_pattern)
        self.groups_dir = Path(groups_dir)
        self.song_order = song_order
        self.song_glob = song_glob
        self.song_pattern = re.compile(song_pattern)
        self.repeat = repeat

    def next_songs(self) -> Iterable[Song]:
        while True:
            is_fresh = False
            if self._groups is None:
                self._groups = self._find_groups()
                is_fresh = True
            for group in self._groups:
                log.debug(f'checking group "{group!s}"')
                for song_path in self._find_songs(group):
                    log.debug(f'found song "{song_path!s}"')
                    yield Song.from_path(song_path)
                return
            else: # no more groups left
                if not self.repeat:
                    return
                self._groups = None # set up reload
                if is_fresh: # just reloaded & already exhausted
                    return

    @classmethod
    def with_name(Class, name: str) -> 'FancyPlaylist':
        settings = dataIO.load_json(Class.path(name))
        return Class(name=name, **settings)

    @staticmethod
    def path(name: str) -> Path:
        if not name:
            raise RuntimeError('Playlist has no name.')
        # todo: sanitize playlist name
        return data_path() / 'playlists' / f'{name}.playlist.json'

    def settings(self) -> Dict[str, Any]:
        return {'name': self.name, 'group_order': self.group_order,
                'group_glob': self.group_glob, 'song_glob': self.song_glob,
                'group_pattern': self.group_pattern,
                'groups_dir': self.groups_dir, 'song_order': self.song_order,
                'song_pattern': self.song_pattern, 'repeat': self.repeat}

    def save(self) -> None:
        dataIO.save_json(self.path(self.name), self.settings())

    def _rejigger(self, name: str) -> Callable[[Iterable], Iterable]:
        rejiggers = {'sort': sorted, 'shuffle': shuffled}
        return rejiggers.get(name, lambda x: x)

    def _find_groups(self) -> Iterable[Path]:
        groups = (group for group in self.groups_dir.glob(self.group_glob)
                  if group.is_dir() and self.group_pattern.search(group.as_posix()))
        rejiggered = self._rejigger(self.group_order)
        yield from rejiggered(groups)

    def _find_songs(self, group: Path) -> Iterable[Path]:
        songs = (song for song in group.glob(self.song_glob)
                 if song.is_file() and self.song_pattern.search(song.as_posix()))
        rejiggered = self._rejigger(self.song_order)
        yield from rejiggered(songs)


def shuffled(iter: Iterable) -> Iterable:
    items = list(iter)
    while items:
        yield items.pop(random.randrange(len(items)))
