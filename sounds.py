#!/usr/bin/env python3

"""
Teletype sounds
(requires pygame)
"""

import os
import random
import logging
import pygame
from pygame.mixer import Sound


logger = logging.getLogger(__name__)


class PygameSounds:
    """Teletype sounds, using pygame mixer"""

    # Some events
    EVENT_HUM = pygame.USEREVENT+2
    EVENT_KEY = pygame.USEREVENT+3
    EVENT_CHR = pygame.USEREVENT+4
    EVENT_SYNC = pygame.USEREVENT+5
    EVENTS = [EVENT_HUM, EVENT_KEY, EVENT_CHR, EVENT_SYNC]

    def __init__(self):
        pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=512)
        # Load the sounds into a dict for easy access
        self.sounds = {}
        # Start with the lid up (close the lid with F7 if you want peace and quiet)
        self.lid_state = "up"
        # Channels for playback
        self.ch0 = None
        self.ch1 = None
        self.ch2 = None
        # Channels for effects
        self._chfx = [None, None, None]
        # The current channel for effects
        self._fx = 0
        # Sounds that we keep using
        self.hum_sound = None
        self.spaces_sound = None
        self.chars_sound = None
        # How many keypresses are queued (including current)
        self.active_key_count = 0
        # What characters are queued to print (including current)
        self.active_printout = ""

    def get(self, sound_name):
        # Get a sound by name.
        # All sounds depend on whether the lid is open, that's part of the name.
        actual_name = self.lid_state + "-" + sound_name
        if sound_name in self.sounds:
            return self.sounds[actual_name]
        # There are some 'repeated sample' sounds (e.g. keys) where we choose one at random from the set
        sounds = [sound for name, sound in self.sounds.items() if name.startswith(actual_name)]
        return random.sample(sounds, 1)[0]

    def start(self):
        # Load the sound library
        with os.scandir(path=os.path.join(os.path.dirname(__file__), "sounds")) as scan:
            for entry in scan:
                if entry.is_file:
                    filename, ext = os.path.splitext(entry.name)
                    if ext == ".wav":
                        self.sounds[filename] = Sound(entry.path)

        pygame.mixer.set_reserved(6)
        self.ch0 = pygame.mixer.Channel(0)  # used for on/off, background hum, lid
        self.ch1 = pygame.mixer.Channel(1)  # printing spaces (loop)
        self.ch2 = pygame.mixer.Channel(2)  # printing characters (loop)
        self._chfx = [  # fx: input (keypresses), platen, bells, etc
            pygame.mixer.Channel(3),
            pygame.mixer.Channel(4),
            pygame.mixer.Channel(5)
        ]

        self.hum_sound = self.get("hum")
        self.spaces_sound = self.get("print-spaces")
        self.chars_sound = self.get("print-chars")

        # Play the power-on sound, then to background after 1.5
        self.ch0.play(self.get("motor-on"))
        pygame.time.set_timer(self.EVENT_HUM, 1500)
        pygame.time.wait(1000)

    @property
    def chfx(self):
        # Get a channel for effects
        for i in range(len(self._chfx)):
            channel = self._chfx[i]
            if not channel.get_busy():
                self._fx = i
                return channel
        self._fx = (self._fx + 1) % len(self._chfx)
        return self._chfx[self._fx]

    def stop(self):
        # Play the power-off sound
        self.ch0.play(self.get("motor-off"))
        # Wait until it plays out a bit
        pygame.time.wait(500)
        # Fade out over 1 second
        self.ch0.fadeout(1000)
        pygame.time.wait(1000)

    def lid(self):
        """Open or close the lid."""
        # This just plays on the background channel.
        logger.debug("lid")
        self.ch0.play(self.get("lid"))
        # Flip the lid state
        if self.lid_state == "down":
            self.lid_state = "up"
        else:
            self.lid_state = "down"
        # The main sounds will change with the new lid position
        pygame.time.set_timer(self.EVENT_HUM, 250)

    def platen(self):
        """Hand-scrolled platen for page up & down"""
        logger.debug("platen")
        self.chfx.play(self.get("platen"))

    def _start_loops(self):
        self.hum_sound = self.get("hum")
        self.spaces_sound = self.get("print-spaces")
        self.chars_sound = self.get("print-chars")
        self.ch0.play(self.hum_sound, loops=-1)
        self.ch1.play(self.spaces_sound, loops=-1)
        self.ch2.play(self.chars_sound, loops=-1)

    def keypress(self, key):
        """Key pressed at the keyboard (may or may not echo)"""
        logger.debug("keypress")
        self.active_key_count = self.active_key_count + 1
        if self.active_key_count > 1:
            # Just queue it and keep going
            return
        # Press any key (they all sound similar)
        self.chfx.play(self.get("key"))
        # In a while we can press another key
        pygame.time.set_timer(self.EVENT_KEY, 100)
        self._sound_for_keypress()

    def _sound_for_keypress(self):
        if self.active_key_count <= 0:
            # No next keypress.  Cancel the timer.
            pygame.time.set_timer(self.EVENT_KEY, 0)
        else:
            self.chfx.play(self.get("key"))

    def print_chars(self, chars):
        logger.debug("print: %s", chars)
        # Add to the string that we're printing
        self.active_printout = self.active_printout + chars
        # Set the print timer for 100ms (repeats)
        pygame.time.set_timer(self.EVENT_CHR, 100)
        self._sound_for_char()

    def _sound_for_char(self):
        next_char = self.active_printout[:1]
        if next_char == "":
            # No next character.  Go back to hum.
            pygame.time.set_timer(self.EVENT_CHR, 0)
            pygame.time.set_timer(self.EVENT_HUM, 100)
        elif next_char == '\r':
            # Carriage return (not newline, that just sounds as a space)
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(0.7)
            self.chars_sound.set_volume(0.0)
            # Reset the loop timing so it synchronizes better with the CR
            self.chfx.play(self.get("cr"))
            pygame.time.set_timer(self.EVENT_SYNC, 10)
        elif next_char == '\007':
            # Mute the hum/print while we do this
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(0.0)
            self.chfx.play(self.get("bell"))
        elif ord(next_char) <= 32 or next_char.isspace():
            # Control characters and spaces
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(1.0)
            self.chars_sound.set_volume(0.0)
        else:
            # Treat anything else as printable
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(1.0)

    def event(self, evt):
        # A pygame event happened
        if evt == self.EVENT_HUM:
            logger.debug("EVENT_HUM")
            # Cancel the hum timer
            pygame.time.set_timer(self.EVENT_HUM, 0)
            # Background hum (unless there's print pending)
            if self.active_printout:
                # No hum yet, we're printing
                return
            # Go back to playing the hum on loop, and reset the spaces/chars loops
            self._start_loops()
            self.hum_sound.set_volume(1.0)
            self.spaces_sound.set_volume(0.0)
            self.chars_sound.set_volume(0.0)

        elif evt == self.EVENT_KEY:
            logger.debug("EVENT_KEY")
            self.active_key_count = self.active_key_count - 1
            self._sound_for_keypress()

        elif evt == self.EVENT_CHR:
            self.active_printout = self.active_printout[1:]
            self._sound_for_char()

        elif evt == self.EVENT_SYNC:
            pygame.time.set_timer(self.EVENT_SYNC, 0)
            self._start_loops()
            self.hum_sound.set_volume(0.0)
            self.spaces_sound.set_volume(1.0)
            self.chars_sound.set_volume(0.0)

        else:
            logger.debug("Event: %s", evt)
