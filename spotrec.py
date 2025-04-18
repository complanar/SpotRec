#!/usr/bin/python3

# License: https://raw.githubusercontent.com/Bleuzen/SpotRec/master/LICENSE

import dbus
from dbus.exceptions import DBusException
import dbus.mainloop.glib
from gi.repository import GLib
from pathlib import Path

from threading import Thread
import subprocess
import time
import sys
import shutil
import re
import os
import argparse
import traceback
import logging
import shlex
import requests

# Deps:
# 'python'
# 'python-dbus'
# 'ffmpeg'
# 'gawk': awk in command to get sink input id of spotify
# 'pulseaudio': sink control stuff
# 'bash': shell commands
# 'requests': get album art

# TODO:
# - set fixed latency on pipewire (currently only done by ffmpeg while it is recording ("fragment_size" parameter), but should ideally be set before recording)

app_name = "SpotRec"
app_version = "0.15.1"

# Settings with Defaults
_debug_logging = False
_skip_intro = False
_mute_pa_recording_sink = False
_output_directory = f"{Path.home()}/{app_name}"
_filename_pattern = "{trackNumber} - {artist} - {title}"
_underscored_filenames = False
_use_internal_track_counter = False
_add_cover_art = False

# Hard-coded settings
_pa_recording_sink_name = "spotrec"
_pa_max_volume = "65536"
_recording_time_before_song = 0.25
_recording_time_after_song = 1.25
_playback_time_before_seeking_to_beginning = 5.0
_playback_time_before_skipping_to_next = 1.0
_recording_minimum_time = 8.0 # this should be longer than _playback_time_before_seeking_to_beginning
_shell_executable = "/bin/bash"  # Default: "/bin/sh"
_shell_encoding = "utf-8"
_ffmpeg_executable = "ffmpeg"  # Example: "/usr/bin/ffmpeg"

# Variables that change during runtime
is_script_paused = False
is_first_playing = True
pa_spotify_sink_input_id = -1
internal_track_counter = 1
recorded_tracks = {}
is_shutting_down = False


def main():
    handle_command_line()

    if not _skip_intro:
        print(app_name + " v" + app_version)
        print("You should not pause, seek or change volume during recording!")
        print("Existing files will be overridden!")
        print("Use --help as argument to see all options.")
        print()
        print("Disclaimer:")
        print('This software is for "educational" purposes only. No responsibility is held or accepted for misuse.')
        print()
        print("Output directory:")
        print(_output_directory)
        print()

    init_log()

    # Create the output directory
    Path(_output_directory).mkdir(
        parents=True, exist_ok=True)

    # Init Spotify DBus listener
    global _spotify
    _spotify = Spotify()

    # Load PulseAudio sink
    PulseAudio.load_sink()

    _spotify.init_pa_stuff_if_needed()

    # Keep the main thread alive (to be able to handle KeyboardInterrupt)
    while True:
        time.sleep(1)


def doExit():
    log.info(f"[{app_name}] Shutting down ...")

    global is_shutting_down
    is_shutting_down = True

    # Stop Spotify DBus listener
    _spotify.quit_glib_loop()

    # Kill all FFmpeg subprocesses
    FFmpeg.killAll()

    # Unload PulseAudio sink
    PulseAudio.unload_sink()

    log.info(f"[{app_name}] Bye")

    # Have to use os exit here, because otherwise GLib would print a strange error message
    os._exit(0)
    # sys.exit(0)


def handle_command_line():
    global _debug_logging
    global _skip_intro
    global _mute_pa_recording_sink
    global _output_directory
    global _filename_pattern
    global _underscored_filenames
    global _use_internal_track_counter
    global _add_cover_art

    parser = argparse.ArgumentParser(
        description=app_name + " v" + app_version, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-d", "--debug", help="Print a little more",
                        action="store_true", default=_debug_logging)
    parser.add_argument("-s", "--skip-intro", help="Skip the intro message",
                        action="store_true", default=_skip_intro)
    parser.add_argument("-m", "--mute-recording", help="Mute Spotify on your main output device while recording",
                        action="store_true", default=_mute_pa_recording_sink)
    parser.add_argument("-o", "--output-directory", help="Where to save the recordings\n"
                                                         "Default: " + _output_directory, default=_output_directory)
    parser.add_argument("-p", "--filename-pattern", help="A pattern for the file names of the recordings\n"
                                                         "Available: {artist}, {album}, {trackNumber}, {title}\n"
                                                         "Default: \"" + _filename_pattern + "\"\n"
                                                         "May contain slashes to create sub directories\n"
                                                         "Example: \"{artist}/{album}/{trackNumber} {title}\"", default=_filename_pattern)
    parser.add_argument("-u", "--underscored-filenames", help="Force the file names to have underscores instead of whitespaces",
                        action="store_true", default=_underscored_filenames)
    parser.add_argument("-c", "--internal-track-counter", help="Replace Spotify's trackNumber with own counter. Useable for preserving a playlist file order",
                        action="store_true", default=_use_internal_track_counter)
    parser.add_argument("-a", "--add-cover-art", help="Embed the cover art from Spotify into the file",
                        action="store_true", default=_add_cover_art)

    args = parser.parse_args()

    _debug_logging = args.debug

    _skip_intro = args.skip_intro

    _mute_pa_recording_sink = args.mute_recording

    _filename_pattern = args.filename_pattern

    _output_directory = args.output_directory

    _underscored_filenames = args.underscored_filenames

    _use_internal_track_counter = args.internal_track_counter

    _add_cover_art = args.add_cover_art


def init_log():
    global log
    log = logging.getLogger()

    if _debug_logging:
        FORMAT = '%(asctime)-15s - %(levelname)s - %(message)s'
        log.setLevel(logging.DEBUG)
    else:
        FORMAT = '%(message)s'
        log.setLevel(logging.INFO)

    logging.basicConfig(format=FORMAT)

    log.debug("Logger initialized")


class Spotify:
    dbus_dest = "org.mpris.MediaPlayer2.spotify"
    dbus_path = "/org/mpris/MediaPlayer2"
    mpris_player_string = "org.mpris.MediaPlayer2.Player"

    def __init__(self):
        self.glibloop = None

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        try:
            # Connect to Spotify client dbus interface
            bus = dbus.SessionBus()
            player = bus.get_object(self.dbus_dest, self.dbus_path)
            self.iface = dbus.Interface(
                player, "org.freedesktop.DBus.Properties")
            # Pull the metadata of the current track from Spotify
            self.pull_metadata()
            # Update own metadata vars for current track
            self.update_metadata()
        except DBusException:
            log.error(
                f"Error: Could not connect to the Spotify Client. It has to be running first before starting {app_name}.")
            sys.exit(1)
            pass

        self.track = self.get_track()
        self.trackid = self.metadata.get(dbus.String(u'mpris:trackid'))
        self.playbackstatus = self.iface.Get(
            self.mpris_player_string, "PlaybackStatus")

        self.iface.connect_to_signal(
            "PropertiesChanged", self.on_playing_uri_changed)

        class DBusListenerThread(Thread):
            def __init__(self, parent, *args):
                Thread.__init__(self)
                self.parent = parent

            def run(self):
                # Run the GLib event loop to process DBus signals as they arrive
                self.parent.glibloop = GLib.MainLoop()
                self.parent.glibloop.run()

                # run() blocks this thread. This gets printed after it's dead.
                log.info(f"[{app_name}] GLib Loop thread killed")

        dbuslistener = DBusListenerThread(self)
        dbuslistener.start()

        log.info(f"[{app_name}] Spotify DBus listener started")

        log.info(f"[{app_name}] Current song: {self.track}")
        log.info(f"[{app_name}] Current state: " + self.playbackstatus)

    # TODO: this is a dirty solution (uses cmdline instead of python for now)
    def send_dbus_cmd(self, cmd):
        Shell.run('dbus-send --print-reply --dest=' + self.dbus_dest +
                  ' ' + self.dbus_path + ' ' + self.mpris_player_string + '.' + cmd)

    def quit_glib_loop(self):
        if self.glibloop is not None:
            self.glibloop.quit()

        log.info(f"[{app_name}] Spotify DBus listener stopped")

    def get_metadata_for_ffmpeg(self):
        return {
            "artist": self.metadata_artist,
            "album": self.metadata_album,
            "track": self.metadata_trackNumber.lstrip("0"),
            "title": self.metadata_title,
            "cover_url": self.metadata_artUrl,
        }

    def get_track(self):
        if _underscored_filenames:
            filename_pattern = re.sub(" - ", "__", _filename_pattern)
        else:
            filename_pattern = _filename_pattern

        ret = str(filename_pattern.format(
            artist=self.metadata_artist.replace("/", "_"),
            album=self.metadata_album.replace("/", "_"),
            trackNumber=self.metadata_trackNumber,
            title=self.metadata_title.replace("/", "_")
        ))

        if _underscored_filenames:
            ret = ret.replace(".", "").lower()
            ret = re.sub(r"[\s\-\[\]()']+", "_", ret)
            ret = re.sub("__+", "__", ret)

        return ret
    
    def detect_ad(self):
        self.is_ad = self.trackid.startswith("spotify:ad:") or self.trackid.startswith("/com/spotify/ad")
        return self.is_ad

    def is_playing(self):
        return self.playbackstatus == "Playing"

    def start_record(self):
        # Start new recording in new Thread
        class RecordThread(Thread):
            def __init__(self, parent, *args):
                Thread.__init__(self)
                self.parent = parent

            def run(self):
                global is_script_paused
                global recorded_tracks
                global _output_directory

                # Save current trackid to check later if it is still the same song playing (to avoid a bug when user skipped a song)
                self.trackid_when_thread_started = self.parent.trackid

                # Stop the recording before
                # Use copy() to not change the list during this method runs
                self.parent.stop_old_recording(FFmpeg.instances.copy(), self.parent.trackid, self.parent.track)

                # This is currently the only way to seek to the beginning (let it Play for some seconds, Pause and send Previous)
                time.sleep(_playback_time_before_seeking_to_beginning)

                # Check if still the same song is still playing, return if not
                if self.trackid_when_thread_started != self.parent.trackid:
                    return

                # Check if Spotify started looping over a song
                log.debug(recorded_tracks)
                if self.parent.trackid in recorded_tracks.keys():
                    global internal_track_counter

                    internal_track_counter -= 1

                    log.info(
                        f"[{app_name}] Spotify has started looping over a song. Skipping.")
                    time.sleep(_playback_time_before_skipping_to_next)
                    self.parent.send_dbus_cmd("Next")

                    return


                # Spotify pauses when the playlist ended. Don't start a recording / return in this case.
                if not self.parent.is_playing():
                    log.info(
                        f"[{app_name}] Spotify is paused. Maybe the current album or playlist has ended.")

                    # Exit after playlist recorded
                    if not is_script_paused:
                        doExit()

                    return

                # Do not record ads
                if self.parent.is_ad:
                    log.info(f"[{app_name}] Skipping ad")
                    return

                log.info(f"[{app_name}] Starting recording")

                # Set is_script_paused to not trigger wrong Pause event in playbackstatus_changed()
                is_script_paused = True
                # Pause until out dir is created
                self.parent.send_dbus_cmd("Pause")

                # Create output folder if necessary
                # If filename_pattern specifies subfolder(s) the track name is only the basename while the dirname is the subfolder path
                self.out_dir = os.path.join(
                    _output_directory, os.path.dirname(self.parent.track))
                Path(self.out_dir).mkdir(
                    parents=True, exist_ok=True)

                # Go to beginning of the song
                is_script_paused = False
                self.parent.send_dbus_cmd("Previous")

                # Start FFmpeg recording
                ff = FFmpeg()
                ff.record(self.parent.trackid, self.parent.track, time.time(), self.out_dir,
                          self.parent.track, self.parent.get_metadata_for_ffmpeg())

                # Give FFmpeg some time to start up before starting the song
                time.sleep(_recording_time_before_song)

                # Play the track
                self.parent.send_dbus_cmd("Play")

        record_thread = RecordThread(self)
        record_thread.start()

    def stop_old_recording(self, instances, track_id, track_title):
        # Stop the oldest FFmpeg instance (from recording of song before) (if one is running)
        for i in range(len(instances)):
            class OverheadRecordingStopThread(Thread):
                def run(self):
                    global recorded_tracks

                    # Save recorded track ids to recognize spotify looping over a song
                    # only save if recording is longer than [recording_minimum_time] seconds
                    start_time = instances[i].start_time
                    stop_time = time.time()
                    duration = stop_time - start_time
                    if duration >= _recording_minimum_time:
                        recorded_tracks[f"{instances[i].track_id}"] = instances[i].track_title
                        log.info(f"[{app_name}] recording finished: \"{track_title}\"")

                    # Record a little longer to not miss something
                    time.sleep(_recording_time_after_song)

                    # Stop the recording
                    instances[i].stop_blocking()

            overhead_recording_stop_thread = OverheadRecordingStopThread()
            overhead_recording_stop_thread.start()

    # This gets called whenever Spotify sends the playingUriChanged signal
    def on_playing_uri_changed(self, Player, three, four):
        # Pull updated metadata from Spotify
        self.pull_metadata()

        # Update track & trackid
        new_trackid = self.metadata.get(dbus.String(u'mpris:trackid'))
        if self.trackid != new_trackid:
            # Update internal track metadata vars
            self.update_metadata()
            # Update trackid
            self.trackid = new_trackid
            # Update Ad detection
            self.detect_ad()
            # Update track name
            self.track = self.get_track()
            # Trigger event method
            self.playing_song_changed()
            # Update internal track counter, do not count ads and already recorded tracks
            if _use_internal_track_counter and not self.is_ad and new_trackid not in recorded_tracks.keys():
                global internal_track_counter
                internal_track_counter += 1

        # Update playback status
        new_playbackstatus = self.iface.Get(Player, "PlaybackStatus")
        if self.playbackstatus != new_playbackstatus:
            self.playbackstatus = new_playbackstatus
            self.playbackstatus_changed()

    def playing_song_changed(self):
        log.info("[Spotify] Song changed: " + self.track)

        self.start_record()

    def playbackstatus_changed(self):
        log.info("[Spotify] State changed: " + self.playbackstatus)

        self.init_pa_stuff_if_needed()

    def pull_metadata(self):
        self.metadata = self.iface.Get(self.mpris_player_string, "Metadata")

    def update_metadata(self):
        self.metadata_artist = ", ".join(
            self.metadata.get(dbus.String(u'xesam:artist')))
        self.metadata_album = self.metadata.get(dbus.String(u'xesam:album'))
        self.metadata_title = self.metadata.get(dbus.String(u'xesam:title'))
        self.metadata_trackNumber = str(self.metadata.get(
            dbus.String(u'xesam:trackNumber'))).zfill(2)
        # https://github.com/patrickziegler/SpotifyRecorder/blob/4c1cc0a5449d0ca8bfb409ef98f4c7a21c73fe0f/spotify_recorder/track.py#L88
        # https://community.spotify.com/t5/Desktop-Linux/MPRIS-cover-art-url-file-not-found/m-p/4929877/highlight/true#M19504
        self.metadata_artUrl = str(self.metadata.get(dbus.String(u'mpris:artUrl'))).replace(
            "https://open.spotify.com/image/",
            "https://i.scdn.co/image/"
        )

        if _use_internal_track_counter:
            global internal_track_counter
            self.metadata_trackNumber = str(internal_track_counter).zfill(3)

    def init_pa_stuff_if_needed(self):
        if self.is_playing():
            global is_first_playing
            if is_first_playing:
                is_first_playing = False
                log.debug(f"[{app_name}] Initializing PulseAudio stuff")

                PulseAudio.init_spotify_sink_input_id()
                PulseAudio.set_sink_volumes_to_100()

                PulseAudio.move_spotify_to_own_sink()


class FFmpeg:
    instances = []

    def record(self, track_id: str, track_title: str, start_time: float, out_dir: str, file: str, metadata_for_file={}):
        self.track_id = track_id
        self.track_title = track_title
        self.start_time = start_time
        self.out_dir = out_dir

        self.pulse_input = _pa_recording_sink_name + ".monitor"

        # Use a dot as filename prefix to hide the file until the recording was successful
        self.tmp_file_prefix = "."
        self.filename = self.tmp_file_prefix + \
            os.path.basename(file) + ".flac"

        # save this to self because metadata_params is discarded after this function
        self.cover_url = metadata_for_file.pop('cover_url')
        # build metadata param
        metadata_params = ''
        for key, value in metadata_for_file.items():
            metadata_params += ' -metadata ' + key + '=' + shlex.quote(value)

        # FFmpeg Options:
        #  "-hide_banner": short the debug log a little
        #  "-y": overwrite existing files
        #  "-ac 2": always use 2 audio channels (stereo) (same as Spotify)
        #  "-ar 44100": always use 44.1k samplerate (same as Spotify)
        #  "-fragment_size 8820": set recording latency to 50 ms (0.05*44100*2*2) (very high values can cause ffmpeg to not stop fast enough, so post-processing fails)
        #  "-acodec flac": use the flac lossless audio codec, so we don't lose quality while recording
        self.process = Shell.Popen(_ffmpeg_executable + ' -hide_banner -y '
                                   '-f pulse ' +
                                   '-ac 2 -ar 44100 -fragment_size 8820 ' +
                                   '-i ' + self.pulse_input + metadata_params + ' '
                                   '-acodec flac' +
                                   ' ' + shlex.quote(os.path.join(self.out_dir, self.filename)))

        self.pid = str(self.process.pid)

        self.instances.append(self)

        log.info(f"[FFmpeg] [{self.pid}] Recording started")

    # The blocking version of this method waits until the process is dead
    def stop_blocking(self):
        # Remove from instances list (and terminate)
        if self in self.instances:
            self.instances.remove(self)

            # Send CTRL_C
            self.process.terminate()

            log.info(f"[FFmpeg] [{self.pid}] terminated")

            # Sometimes this is not enough and ffmpeg survives, so we have to kill it after some time
            time.sleep(1)

            if self.process.poll() == None:
                # None means it has no return code (yet), with other words: it is still running

                self.process.kill()

                log.info(f"[FFmpeg] [{self.pid}] killed")
            else:
                global is_shutting_down
                if not is_shutting_down:  # Do not post-process unfinished recordings
                    tmp_file = os.path.join(
                        self.out_dir, self.filename)
                    new_file = os.path.join(self.out_dir,
                                            self.filename[len(self.tmp_file_prefix):])
                    if os.path.exists(tmp_file):
                        shutil.move(tmp_file, new_file)
                        log.debug(
                            f"[FFmpeg] [{self.pid}] Successfully renamed {self.filename}")
                        global _add_cover_art
                        if _add_cover_art:
                            class AddCoverArtThread(Thread):
                                def __init__(self, parent, fullfilepath):
                                    Thread.__init__(self)
                                    self.parent = parent
                                    self.fullfilepath = fullfilepath

                                def run(self):
                                    self.parent.add_cover_art(
                                        self.fullfilepath)

                            add_cover_art_thread = AddCoverArtThread(
                                self, new_file)
                            add_cover_art_thread.start()
                    else:
                        log.warning(
                            f"[FFmpeg] [{self.pid}] Failed renaming {self.filename}")

            # Remove process from memory (and don't left a ffmpeg 'zombie' process)
            self.process = None

    # Kill the process in the background
    def stop(self):
        class KillThread(Thread):
            def __init__(self, parent, *args):
                Thread.__init__(self)
                self.parent = parent

            def run(self):
                self.parent.stop_blocking()

        kill_thread = KillThread(self)
        kill_thread.start()

    # add cover art to temp _withArtwork file
    # and then move it to replace the original file
    def add_cover_art(self, fullfilepath):
        if self.cover_url is None:
            log.debug(f'[FFmpeg] No cover art found for {fullfilepath}')
            return
        # save the image locally -> could use a temp file here
        #   but might add option to keep image later
        cover_file = fullfilepath.rsplit(
            '.flac', 1)[0]  # remove the extension
        log.debug(f'Saving cover art to {cover_file} + image_ext')
        temp_file = cover_file + '_withArtwork.' + 'flac'
        if self.cover_url.startswith('file://'):
            log.debug(f'[FFmpeg] Cover art is local for {fullfilepath}')
            path = self.cover_url[len('file://'):]
            _, ext = os.path.splitext(path)
            cover_file += ext
            shutil.copy2(path, cover_file)
        else:
            log.debug(f'[FFmpeg] Cover art is on server for {fullfilepath}')
            answer = requests.get(self.cover_url)
            if not answer.ok:
                log.debug(
                    f'[FFmpeg] Cover art not loaded from server for {fullfilepath}')
                return
            cover_file += "." + answer.headers["Content-Type"].rsplit("/")[-1]
            with open(cover_file, "wb") as fd:
                fd.write(answer.content)
        # add it to a temporary file
        log.debug(f'[FFmpeg] Merging cover art into {fullfilepath}')
        # no need for separate thread / logging here because quick
        returncode = Shell.run(_ffmpeg_executable + ' ' +
                               '-y -i {} -i {} -map 0:a -map 1 '.format(
                                   shlex.quote(fullfilepath), shlex.quote(cover_file)) +
                               '-codec copy -id3v2_version 3 ' +
                               '-metadata:s:v title="Album cover" ' +
                               '-metadata:s:v comment="Cover (front)" ' +
                               '-disposition:v attached_pic ' +
                               shlex.quote(temp_file)).returncode
        if returncode != 0:
            log.warning(f"[FFmpeg] Failed adding artwork to {fullfilepath}")
            return
        # overwrite the actual file by the temp file
        log.debug(
            f'[FFmpeg] Added cover art for {fullfilepath} in temp file, moving it')
        shutil.move(temp_file, fullfilepath)
        os.remove(cover_file)   # now delete the cover art

    @staticmethod
    def killAll():
        log.info("[FFmpeg] Killing all instances")

        # Run as long as list ist not empty
        while FFmpeg.instances:
            FFmpeg.instances[0].stop_blocking()

        log.info("[FFmpeg] All instances killed")


class Shell:
    @staticmethod
    def run(cmd):
        # 'run()' waits until the process is done
        log.debug(f"[Shell] run: {cmd}")
        if _debug_logging:
            return subprocess.run(cmd.encode(_shell_encoding), stdin=None, shell=True, executable=_shell_executable, encoding=_shell_encoding)
        else:
            with open("/dev/null", "w") as devnull:
                return subprocess.run(cmd.encode(_shell_encoding), stdin=None, stdout=devnull, stderr=devnull, shell=True, executable=_shell_executable, encoding=_shell_encoding)

    @staticmethod
    def Popen(cmd):
        # 'Popen()' continues running in the background
        log.debug(f"[Shell] Popen: {cmd}")
        if _debug_logging:
            return subprocess.Popen(cmd.encode(_shell_encoding), stdin=None, shell=True, executable=_shell_executable, encoding=_shell_encoding)
        else:
            with open("/dev/null", "w") as devnull:
                return subprocess.Popen(cmd.encode(_shell_encoding), stdin=None, stdout=devnull, stderr=devnull, shell=True, executable=_shell_executable, encoding=_shell_encoding)

    @staticmethod
    def check_output(cmd):
        log.debug(f"[Shell] check_output: {cmd}")
        out = subprocess.check_output(cmd.encode(
            _shell_encoding), shell=True, executable=_shell_executable, encoding=_shell_encoding)
        # when not using 'encoding=' -> out.decode()
        # but since it is set, decode() ist not needed anymore
        # out = out.decode()
        return out.rstrip('\n')


class PulseAudio:
    sink_id = ""

    @staticmethod
    def load_sink():
        log.info(f"[{app_name}] Creating pulse sink")

        if _mute_pa_recording_sink:
            PulseAudio.sink_id = Shell.check_output('pactl load-module module-null-sink sink_name="' + _pa_recording_sink_name +
                                                    '" sink_properties=device.description="' + _pa_recording_sink_name + '" rate=44100 channels=2')
        else:
            PulseAudio.sink_id = Shell.check_output('pactl load-module module-remap-sink sink_name="' + _pa_recording_sink_name +
                                                    '" sink_properties=device.description="' + _pa_recording_sink_name + '" rate=44100 channels=2 remix=no')
            # To use another master sink where to play:
            # pactl load-module module-remap-sink sink_name=spotrec sink_properties=device.description="spotrec" master=MASTER_SINK_NAME channels=2 remix=no

    @staticmethod
    def unload_sink():
        log.info(f"[{app_name}] Unloading pulse sink")
        Shell.run('pactl unload-module ' + PulseAudio.sink_id)

    @staticmethod
    def init_spotify_sink_input_id():
        global pa_spotify_sink_input_id

        if pa_spotify_sink_input_id > -1:
            return

        application_name = "spotify"
        cmdout = Shell.check_output(
            "pactl list sink-inputs | awk '{print tolower($0)};' | awk '/ #/ {print $0} /application.name = \"" + application_name + "\"/ {print $3};'")
        index = -1
        last = ""

        for line in cmdout.split('\n'):
            if line == '"' + application_name + '"':
                index = last.split(" #", 1)[1]
                break
            last = line

        # Alternative command:
        # for i in $(LC_ALL=C pactl list | grep -E '(^Sink Input)|(media.name = \"Spotify\"$)' | cut -d \# -f2 | grep -v Spotify); do echo "$i"; done

        pa_spotify_sink_input_id = int(index)

    @staticmethod
    def move_spotify_to_own_sink():
        class MoveSpotifyToSinktThread(Thread):
            def run(self):
                if pa_spotify_sink_input_id > -1:
                    exit_code = Shell.run("pactl move-sink-input " + str(
                        pa_spotify_sink_input_id) + " " + _pa_recording_sink_name).returncode

                    if exit_code == 0:
                        log.info(f"[{app_name}] Moved Spotify to own sink")
                    else:
                        log.warning(
                            f"[{app_name}] Failed to move Spotify to own sink")

        move_spotify_to_sink_thread = MoveSpotifyToSinktThread()
        move_spotify_to_sink_thread.start()

    @staticmethod
    def set_sink_volumes_to_100():
        log.debug(f"[{app_name}] Set sink volumes to 100%")

        # Set Spotify volume to 100%
        Shell.Popen("pactl set-sink-input-volume " +
                    str(pa_spotify_sink_input_id) + " " + _pa_max_volume)

        # Set recording sink volume to 100%
        Shell.Popen("pactl set-sink-volume " +
                    _pa_recording_sink_name + " " + _pa_max_volume)


if __name__ == "__main__":
    # Handle exit (not print error when pressing Ctrl^C)
    try:
        main()
    except KeyboardInterrupt:
        doExit()
    except Exception:
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    sys.exit(0)
