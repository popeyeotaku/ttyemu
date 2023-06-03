#!/usr/bin/env python3
"""Teletype sounds.

(requires pygame)
"""

import os
import random
import logging
import pygame
from pygame.mixer import Sound


logger = logging.getLogger(__name__)


class PygameSounds:
    """Teletype sounds, using pygame mixer."""

    # Some events
    EVENT_HUM = pygame.USEREVENT + 2
    EVENT_KEY = pygame.USEREVENT + 3
    EVENT_CHR = pygame.USEREVENT + 4
    EVENT_SYNC = pygame.USEREVENT + 5
    EVENTS = [EVENT_HUM, EVENT_KEY, EVENT_CHR, EVENT_SYNC]

    def __init__(self):
        """Create the class."""
        pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=512)
        # Load the sounds into a dict for easy access
        self.sounds: dict[str, pygame.mixer.Sound] = {}
        # Start with the lid up (close the lid with F7 if you want peace and quiet)
        self.lid_state = "up"
        # Channels for playback
        self.ch0: None | pygame.mixer.Channel = None
        self.ch1: None | pygame.mixer.Channel = None
        self.ch2: None | pygame.mixer.Channel = None
        # Channels for effects
        self._chfx: list[None | pygame.mixer.Channel] = [None, None, None]
        # The current channel for effects
        self._fx: int = 0
        # Sounds that we keep using
        self.hum_sound: None | pygame.mixer.Sound = None
        self.spaces_sound: None | pygame.mixer.Sound = None
        self.chars_sound: None | pygame.mixer.Sound = None
        # How many keypresses are queued (including current)
        self.active_key_count: int = 0
        # What characters are queued to print (including current)
        self.active_printout: str = ""

    def get(self, sound_name: str) -> pygame.mixer.Sound:
        """Get a sound by name.

        All sounds depend on whether the lid is open, that's part of the name.
        """
        actual_name = self.lid_state + "-" + sound_name
        if sound_name in self.sounds:
            return self.sounds[actual_name]
        # There are some 'repeated sample' sounds (e.g. keys) where we choose one at random from the set
        sounds = [
            sound for name, sound in self.sounds.items() if name.startswith(actual_name)
        ]
        return random.sample(sounds, 1)[0]

    def start(self) -> None:
        """Load the sound library."""
        try:
            with os.scandir(
                path=os.path.join(os.path.dirname(__file__), "sounds")
            ) as scan:
                for entry in scan:
                    if entry.is_file():
                        filename, ext = os.path.splitext(entry.name)
                        if ext == ".wav":
                            self.sounds[filename] = Sound(entry.path)
        except pygame.error:
            logging.exception("Could not initialize sounds.")
            return
        if not self.sounds:
            logging.error("Could not load sounds.")
            return

        pygame.mixer.set_reserved(6)
        self.ch0 = pygame.mixer.Channel(0)  # used for on/off, background hum, lid
        self.ch1 = pygame.mixer.Channel(1)  # printing spaces (loop)
        self.ch2 = pygame.mixer.Channel(3)  # printing characters (loop)
        self._chfx = [  # fx: input (keypresses), platen, bells, etc
            pygame.mixer.Channel(3),
            pygame.mixer.Channel(4),
            pygame.mixer.Channel(5),
        ]

        self.hum_sound = self.get("hum")
        self.spaces_sound = self.get("print-spaces")
        self.chars_sound = self.get("print-chars")

        # Play the power-on sound, then to background after 1.5
        self.ch0.play(self.get("motor-on"))
        pygame.time.set_timer(self.EVENT_SYNC, 1500)
        pygame.time.wait(1000)
        self._start_paused()

    @property
    def chfx(self) -> pygame.mixer.Channel:
        """Get a channel for effects."""
        for i in range(len(self._chfx)):
            channel = self._chfx[i]
            assert channel is not None
            if not channel.get_busy():
                self._fx = i
                return channel
        self._fx = (self._fx + 1) % len(self._chfx)
        grabbed = self._chfx[self._fx]
        assert grabbed is not None
        return grabbed

    def stop(self):
        """Wrap-up the sounds system."""
        if not self.sounds:
            return
        assert self.ch0 is not None
        # Play the power-off sound
        self.ch0.play(self.get("motor-off"))
        # Wait until it plays out a bit
        pygame.time.wait(500)
        # Fade out over 1 second
        self.ch0.fadeout(1000)
        pygame.time.wait(1000)

    def lid(self):
        """Open or close the lid."""
        if not self.sounds:
            return
        logger.debug("lid")
        self._fade_to_hum()
        self.chfx.play(self.get("lid"))
        # Flip the lid state
        if self.lid_state == "down":
            self.lid_state = "up"
        else:
            self.lid_state = "down"
        # The main sounds will change with the new lid position
        pygame.time.set_timer(self.EVENT_SYNC, 250)

    def platen(self):
        """Hand-scrolled platen for page up & down."""
        if not self.sounds:
            return
        logger.debug("platen")
        self.chfx.play(self.get("platen"))

    def _start_loops(self):
        """Start all looping sounds."""
        assert self.ch0 is not None and self.ch1 is not None and self.ch2 is not None
        self.hum_sound = self.get("hum")
        self.spaces_sound = self.get("print-spaces")
        self.chars_sound = self.get("print-chars")
        self.ch0.play(self.hum_sound, loops=-1)
        self.ch1.play(self.spaces_sound, loops=-1)
        self.ch2.play(self.chars_sound, loops=-1)
        self.hum_sound.set_volume(0.0)
        self.spaces_sound.set_volume(0.0)
        self.chars_sound.set_volume(0.0)

    def _start_paused(self):
        """Start all looping sounds, pausing them."""
        self._start_loops()
        assert self.ch1 is not None and self.ch2 is not None
        self.ch1.pause()
        self.ch2.pause()

    def keypress(self, key: str) -> None:
        """Register a key pressed at the keyboard (may or may not echo)."""
        if not self.sounds:
            return
        logger.debug("keypress")
        self.active_key_count = self.active_key_count + 1
        if self.active_key_count > 1:
            # Just queue it and keep going
            return
        # In a while we can press another key
        pygame.time.set_timer(self.EVENT_KEY, 100)
        self._sound_for_keypress()

    def _sound_for_keypress(self) -> None:
        """Play a keypress sound."""
        if self.active_key_count <= 0:
            # No next keypress.  Cancel the timer.
            pygame.time.set_timer(self.EVENT_KEY, 0)
        else:
            # Press any key (they all sound similar)
            self.chfx.play(self.get("key"))

    def print_chars(self, chars: str) -> None:
        """Print a series of characters."""
        if not self.sounds:
            return
        logger.debug("print: %s", chars)
        # Add to the string that we're printing
        self.active_printout = self.active_printout + chars
        # Set the print timer for 100ms (repeats)
        pygame.time.set_timer(self.EVENT_CHR, 100)
        self._sound_for_char()

    def _sound_for_char(self):
        """Play a sound for the next printed character."""
        next_char = self.active_printout[:1]
        assert self.hum_sound is not None
        assert self.spaces_sound is not None
        assert self.chars_sound is not None
        if next_char == "":
            # No next character.  Go back to hum.
            pygame.time.set_timer(self.EVENT_CHR, 0)
            pygame.time.set_timer(self.EVENT_HUM, 100)
        elif next_char == "\r":
            # Carriage return (not newline, that just sounds as a space)
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(1.0)
            self.chars_sound.set_volume(0.0)
            self.chfx.play(self.get("cr"))
            # Reset the loop timing
            pygame.time.set_timer(self.EVENT_SYNC, 10)
        elif next_char == "\007":
            # Mute the hum/print while we do this
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(0.0)
            self.chfx.play(self.get("bell"))
        elif ord(next_char) <= 32 or next_char.isspace():
            # Control characters and spaces
            self._fade_to_spaces()
        else:
            # Treat anything else as printable
            self._fade_to_chars()

    def _fade_to_hum(self):
        assert self.hum_sound is not None
        assert self.spaces_sound is not None
        assert self.chars_sound is not None
        if self.hum_sound.get_volume() <= 0.99:
            self.hum_sound.set_volume(0.3)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(0.5)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(0.7)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(1.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(0.0)
        assert self.ch1 is not None
        assert self.ch2 is not None
        self.ch1.pause()
        self.ch2.pause()

    def _fade_to_spaces(self):
        assert self.hum_sound is not None
        assert self.spaces_sound is not None
        assert self.chars_sound is not None
        if self.spaces_sound.get_volume() <= 0.99:
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(0.3)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(0.5)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(0.7)
            self.chars_sound.set_volume(self.chars_sound.get_volume() * 0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(1.0)
            self.chars_sound.set_volume(0.0)
        assert self.ch1 is not None
        assert self.ch2 is not None
        self.ch1.unpause()
        self.ch2.unpause()

    def _fade_to_chars(self):
        assert self.hum_sound is not None
        assert self.spaces_sound is not None
        assert self.chars_sound is not None
        if self.chars_sound.get_volume() <= 0.99:
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(0.3)
            pygame.time.wait(3)
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(0.5)
            pygame.time.wait(3)
            self.hum_sound.set_volume(self.hum_sound.get_volume() * 0.7)
            self.spaces_sound.set_volume(self.spaces_sound.get_volume() * 0.7)
            self.chars_sound.set_volume(0.7)
            pygame.time.wait(3)
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(1.0)
        assert self.ch1 is not None
        assert self.ch2 is not None
        self.ch1.unpause()
        self.ch2.unpause()

    def event(self, evt: int) -> None:
        """Process a pygame event."""
        if not self.sounds:
            return
        if evt == self.EVENT_HUM:
            logger.debug("EVENT_HUM")
            # Cancel the hum timer
            pygame.time.set_timer(self.EVENT_HUM, 0)
            # Background hum (unless there's print pending)
            if self.active_printout:
                # No hum yet, we're printing
                return
            # Go back to playing the hum on loop, and pause the spaces/chars loops
            self._fade_to_hum()

        elif evt == self.EVENT_KEY:
            logger.debug("EVENT_KEY")
            self.active_key_count = self.active_key_count - 1
            self._sound_for_keypress()

        elif evt == self.EVENT_CHR:
            self.active_printout = self.active_printout[1:]
            self._sound_for_char()

        elif evt == self.EVENT_SYNC:
            # Sync after startup and CR: reset the spaces/chars loops.
            pygame.time.set_timer(self.EVENT_SYNC, 0)
            pygame.time.set_timer(self.EVENT_HUM, 100)
            self._start_loops()

        else:
            logger.debug("Event: %s", evt)
