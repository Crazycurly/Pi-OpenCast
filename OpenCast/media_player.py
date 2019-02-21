import logging
import threading
import time
from collections import deque
from pathlib import Path

from dbus import DBusException

from omxplayer import keys
from omxplayer.player import (
    OMXPlayer,
    OMXPlayerDeadError,
)

from .config import config
from .history import History

logger = logging.getLogger(__name__)
config = config['VideoPlayer']


# OmxPlayer documentation: https://elinux.org/Omxplayer
class OmxPlayer(object):
    def __init__(self, default_volume):
        self._stopped = False
        self._queue = deque()
        self._history = History()

        self._volume = default_volume
        self._show_subtitle = True

        self._player = None
        self._autoplay = False
        self._player_mutex = threading.RLock()

        self._cv = threading.Condition()
        self._video_player = threading.Thread(target=self._play_videos)
        self._video_player.start()

    def __del__(self):
        with self._cv:
            self._stopped = True
            if self._playing():
                self.stop()
            else:
                self._cv.notify()

    def play(self, video=None):
        with self._cv:
            self._autoplay = True
            if video is not None:
                self._history.stop_browsing()
                self.queue(video, first=True)
                return

            self._cv.notify()

    def queue(self, video, first=False):
        with self._cv:
            logger.info("[player] queue video: {}".format(video))
            # Position the video with the videos of the same playlist.
            index = 0 if first else len(self._queue)
            if first and video.playlist_id is not None:
                for i, v in enumerate(reversed(self._queue)):
                    if v.playlist_id == video.playlist_id:
                        index = len(self._queue) - i
                        break
            self._queue.insert(index, video)
            logger.debug("[player] queue contains: {}".format(self._queue))
            if self._autoplay:
                self._cv.notify()

    def list_queue(self):
        return list(self._queue)

    def stop(self, stop_browsing=False):
        with self._cv:
            if not self._playing():
                logger.debug("[player] is already stopped")
                return

            if stop_browsing is True:
                self._history.stop_browsing()

            logger.info("[player] stopping ...")
            self._autoplay = False
            self._exec_command('stop')

            def is_stopped():
                return not self._playing()
            if not self._sync(5000, 500, is_stopped):
                logger.error("[player] cannot stop")

    def prev(self):
        with self._cv:
            if not self._history.can_prev():
                return

            if self._playing():
                self.stop()
                self._history.prev()  # Come back on the previously played video
                self.prev()

            self._history.prev()
            self.play()

    def next(self):
        with self._cv:
            if self._playing():
                self.stop()  # Stop and let the player transition to the next video
            self.play()

    def play_pause(self):
        with self._cv:
            if not self._playing():
                self.play()
            else:
                self._exec_command('play_pause')

    def toggle_subtitle(self):
        self._show_subtitle = not self._show_subtitle
        if self._show_subtitle:
            self._exec_command('show_subtitles')
        else:
            self._exec_command('hide_subtitles')

    def increase_subtitle_delay(self):
        self._exec_command('action', keys.INCREASE_SUBTITLE_DELAY)

    def decrease_subtitle_delay(self):
        self._exec_command('action', keys.DECREASE_SUBTITLE_DELAY)

    def change_volume(self, increase):
        with self._cv:
            volume = self._volume
            if increase:
                volume += 0.1
            else:
                volume -= 0.1
            volume = max(min(2, volume), 0)
            if self._exec_command('set_volume', volume):
                self._volume = volume

    def seek(self, forward, long):
        if forward:
            if long:    # Up arrow, + 5 minutes
                self._exec_command('seek', 300)
            else:       # Right arrow, + 30 seconds
                self._exec_command('seek', 30)
        else:
            if long:    # Down arrow, - 5 minutees
                self._exec_command('seek', -300)
            else:       # Left arrow, - 30 seconds
                self._exec_command('seek', -30)

    def _sync(self, timeout, interval, condition):
        step = round(timeout / interval)
        for i in range(step):
            if condition():
                return True
            time.sleep(interval / 1000.0)
        return False

    def _playing(self):
        with self._player_mutex:
            return self._player is not None

    def _reset_player(self):
        with self._player_mutex:
            self._player = None

    def _make_player(self, video):
        command = ['--vol', str(100 * (self._volume - 1.0))]
        for sub in video.subtitles:
            command += ['--subtitles', sub]
        if config.hide_background is True:
            command += ['--blank']

        logger.debug("[player] opening {} with opt: {}".format(video, command))
        self._player = OMXPlayer(video.path,
                                 command,
                                 dbus_name='org.mpris.MediaPlayer2.omxplayer1')
        self._player.exitEvent += self._on_exit

    def _play(self):
        video = None
        if self._history.browsing():
            logger.debug("[player] picking video from history at index ({})"
                         .format(self._history.index()))
            video = self._history.current_item()
        else:
            video = self._queue.popleft()
            self._history.push(video)

        if not Path(video.path).is_file():
            logger.error("[player] file not found: {}".format(video))
            if self._history.browsing():
                self._history.remove(video)
                self._history.stop_browsing()
            return

        with self._player_mutex:
            self._make_player(video)

        def sync_with_bus():
            try:
                self._playing()
                return True
            except (OMXPlayerDeadError, DBusException):
                return False

        # Wait for the DBus interface to be initialised
        if not self._sync(5000, 500, sync_with_bus):
            logger.error("[player] couldn't connect to dbus")
        logger.info("[player] started")

    def _play_videos(self):
        while (True):
            with self._cv:
                while (self._stopped is False
                       and (self._playing() or
                            (len(self._queue) == 0 and
                             not self._history.browsing()))):
                    self._cv.wait()
                if self._stopped:
                    return

                self._play()

    def _exec_command(self, command, *args, **kwargs):
        with self._cv and self._player_mutex:
            if not self._playing():
                return False
            logger.debug("[player] executing command {}".format(command))
            getattr(self._player, command)(*args, **kwargs)
            return True

    def _on_exit(self, player, code):
        # NOTE: No need of using 'with self._cv' as
        # stop synchronizes the destruction.
        logger.info("[player] stopped")
        if self._history.browsing():
            # If there is no next video in the history and no video available in the queue,
            # then if configured, play the last video of the history
            if not (
                config.loop_last and
                self._history.index() is 0 and
                len(self._queue) is 0
            ):
                self._history.next()
        elif config.loop_last and len(self._queue) is 0 and self._autoplay:
            # Push in the queue the last played video
            self._queue.append(self._history.current_item())
        self._reset_player()

        with self._cv:
            if self._autoplay:
                self._cv.notify()


def make_player(default_volume):
    omx_player = OmxPlayer(default_volume)

    return omx_player