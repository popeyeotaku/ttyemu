#!/usr/bin/env python3
"""ASR-33 terminal emulator."""
from io import BufferedIOBase
import socket
import sys
import time
import threading
import tkinter
import tkinter.font
import abc
import subprocess
import logging
import os
import shlex
import asyncio
import telnetlib3
from telnetlib import (
    Telnet,
    IAC,
    WILL,
    WONT,
    DO,
    DONT,
    SB,
    SE,
    BINARY,
    ECHO,
    TTYPE,
    TSPEED,
    NAWS,
)
from typing import Any, Callable

try:
    from typing import Self  # type: ignore
except ImportError:
    from typing_extensions import Self

from paramiko import Channel

try:
    import pty
    import termios

    gotpty = True
except ImportError:
    gotpty = False
try:
    import paramiko
except ImportError:
    pass
try:
    import pygame
    from sounds import PygameSounds
except ImportError:
    pass


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COLUMNS = 72
TEXT_COLOR = (0x33, 0x33, 0x33)


def upper(char: str) -> str:
    """Convert a character to uppercase in the dumbest way possible."""
    ochar = ord(char)
    ochar = (ochar - 32) & 127
    if ochar > 64:
        ochar = 32 | (ochar & 31)
    return chr(ochar + 32)


def background_color() -> tuple[int, int, int]:
    """Return a background color."""
    # Mainly for debug purposes, each new surface
    # gets a new background color in debug mode.
    # r = random.randrange(192, 256)
    # g = random.randrange(192, 256)
    # b = random.randrange(192, 256)
    # return (r, g, b)
    return (0xFF, 0xEE, 0xDD)


class AbstractLine:
    """Efficiently represent a line of text with overstrikes."""

    def __init__(self) -> None:
        """No arguments."""
        self.extents: list[tuple[int, str]] = []

    def place_char(self, column: int, char: str) -> None:
        """Insert a character into an available extent."""
        if char == " ":
            return
        for i, (begin, text) in enumerate(self.extents):
            end = begin + len(text)
            if end == column:
                text = text + char
                self.extents[i] = (begin, text)
            elif end + 1 == column:
                text = text + " " + char
                self.extents[i] = (begin, text)
            # extend left? replace spaces?
        self.extents.append((column, char))

    def string_test(self, chars: str, column: int = 0):
        """Test a given string.

        Insert a sequence of character, interpreting backspace, tab, and
        carriage return. Return value is final column.
        """
        for char in chars:
            if char == "\t":
                column = (column + 8) & -8
            elif char == "\r":
                column = 0
            elif char == "\b":
                column -= 1
            else:
                self.place_char(column, char)
                column += 1
            if column > 71:
                column = 71
            if column < 0:
                column = 0
        return column

    @staticmethod
    def unit_test(chars: str) -> None:
        """Test the class."""
        print("Test of", repr(chars))
        line = AbstractLine()
        line.string_test(chars)
        for begin, text in line.extents:
            print("    ", begin, repr(text))


SLOP = 4


class Frontend(abc.ABC):
    """The frontend - displays and reads in characters."""

    @abc.abstractmethod
    def postchars(self, chars: str) -> None:
        """Output the characters."""
        raise NotImplementedError

    @abc.abstractmethod
    def reinit(self) -> None:
        """Reset the frontend, discarding all state."""
        raise NotImplementedError

    @abc.abstractmethod
    def draw_char(self, line: int, column: int, char: str) -> None:
        """Draw a character from terminal output."""
        raise NotImplementedError

    @abc.abstractmethod
    def lines_screen(self) -> int:
        """Return the number of lines per screen."""
        raise NotImplementedError

    @abc.abstractmethod
    def refresh_screen(
        self, scroll_base: int, cursor_line: int, cursor_column: int
    ) -> None:
        """Refresh the screen."""
        raise NotImplementedError

    @abc.abstractmethod
    def mainloop(self, terminal: "Terminal") -> None:
        """Execute the frontend."""
        raise NotImplementedError


class Backend(abc.ABC):
    """The backend -- the connection we're using."""

    @abc.abstractmethod
    def __init__(self, postchars: Callable[[str], None] = lambda chars: None) -> None:
        """postchars: function to place characters in the output queue."""
        self.postchars = postchars
        self.fast_mode = False

    @abc.abstractmethod
    def write_char(self, char: str) -> None:
        """Write a character from the keyboard to the backend connection."""
        raise NotImplementedError

    @abc.abstractmethod
    def thread_target(self) -> None:
        """Start-up the thread."""
        raise NotImplementedError


class Terminal:
    """Class for keeping track of the terminal state."""

    def __init__(
        self, frontend: Frontend | None = None, backend: Backend | None = None
    ):
        """Create a ASR-33 teletype using a given frontend and backend.

        Backend defaults to a loopback (print what comes in), and frontend
        defaults to a dummy.
        """
        if backend is None:
            backend = LoopbackBackend()
        if frontend is None:
            frontend = DummyFrontend(self)
        self.line: int = 0
        self.column: int = 0
        self.scroll_base: int = 0
        self.max_line: int = 0
        self.frontend: Frontend = frontend
        self.backend: Backend = backend
        self.lines: dict[int, AbstractLine] = {}

    def reinit(self):
        """Discard all state."""
        self.frontend.reinit()
        self.line = 0
        self.column = 0
        self.scroll_base = 0
        self.max_line = 0
        self.lines.clear()

    def alloc_line(self, line: int):
        """Return a line, creating a new one if it doesn't exist."""
        try:
            return self.lines[line]
        except KeyError:
            return self.lines.setdefault(line, AbstractLine())

    def output_char(self, char: str, refresh: bool = True):
        """Simulate a teletype for a single character."""
        # print("output_char", repr(char))
        match char:
            case "\n":
                self.line += 1
            case "\r":
                self.column = 0
            case "\t":
                self.column = (self.column + 7) // 8 * 8
            case "\b":
                self.column -= 1
            case "\f":
                self.reinit()
            case _:
                if char >= " ":
                    char = upper(char)
                    self.alloc_line(self.line).place_char(self.column, char)
                    self.frontend.draw_char(self.line, self.column, char)
                    self.column += 1
        self.constrain_cursor()
        self.scroll_into_view()
        if refresh:
            self.refresh_screen()

    def lines_screen(self) -> int:
        """Return the number of lines on the screen (from front-end)."""
        return self.frontend.lines_screen()

    def refresh_screen(self) -> None:
        """Refresh the screen (to front-end)."""
        self.frontend.refresh_screen(self.scroll_base, self.line, self.column)

    def output_chars(self, chars: str, refresh: bool = True) -> None:
        """Call output_char in a loop without refreshing."""
        for char in chars:
            self.output_char(char, False)
        if refresh:
            self.refresh_screen()

    def constrain_cursor(self) -> None:
        """Ensure cursor is not out of bounds."""
        if self.line < 0:
            self.line = 0
        if self.column < 0:
            self.column = 0
        if self.column >= COLUMNS:
            self.column = COLUMNS - 1

    def scroll_into_view(self, line: int | None = None) -> None:
        """Scroll line into view."""
        if line is None:
            line = self.line
        if line < self.scroll_base:
            self.scroll_base = line
        if line >= self.scroll_base + self.lines_screen():
            self.scroll_base = line - self.lines_screen() + 1

    def page_down(self) -> None:
        """Scroll the page down."""
        self.scroll_base += self.lines_screen() // 2
        self.constrain_scroll()
        self.refresh_screen()

    def page_up(self):
        """Scroll the page up."""
        self.scroll_base -= self.lines_screen() // 2
        self.constrain_scroll()
        self.refresh_screen()

    def constrain_scroll(self):
        """Ensure scroll is in bounds."""
        if self.line > self.max_line:
            self.max_line = self.line
        if self.scroll_base > self.max_line - self.lines_screen() + 1:
            self.scroll_base = self.max_line - self.lines_screen() + 1
        if self.scroll_base < 0:
            self.scroll_base = 0


class TkinterFrontend(Frontend):
    """Front-end using tkinter."""

    # pylint: disable=too-many-instance-attributes
    def __init__(self, terminal: Terminal | None = None) -> None:
        """terminal: the Terminal using this frontend."""
        self.fg = "#%02x%02x%02x" % TEXT_COLOR
        bg = "#%02x%02x%02x" % background_color()
        self.terminal = terminal
        self.root = tkinter.Tk()
        if "Teleprinter" in tkinter.font.families(self.root):
            # http://www.zanzig.com/download/
            font = tkinter.font.Font(family="Teleprinter").actual()
            font["weight"] = "bold"
        elif "TELETYPE 1945-1985" in tkinter.font.families(self.root):
            # https://www.dafont.com/teletype-1945-1985.font
            font = tkinter.font.Font(family="TELETYPE 1945-1985").actual()
        else:
            font = tkinter.font.nametofont("TkFixedFont").actual()
        font["size"] = 16
        font = tkinter.font.Font(**font)
        self.font = font
        self.font_width = font.measure("X")
        self.font_height = self.font_width * 10 / 6
        self.canvas = tkinter.Canvas(
            self.root,
            bg=bg,
            height=24 * self.font_height + SLOP * 2,
            width=COLUMNS * self.font_width + SLOP * 2,
        )
        bbox = (0, 0, self.font_width, self.font_height)
        self.cursor_id = self.canvas.create_rectangle(bbox)
        self.root.bind("<Key>", self.key)
        xscrollbar = tkinter.Scrollbar(self.root, orient="horizontal")
        xscrollbar.grid(row=1, column=0, sticky="ew")
        yscrollbar = tkinter.Scrollbar(self.root)
        yscrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self.max_line = 0
        self.canvas.config(
            xscrollcommand=xscrollbar.set,
            yscrollcommand=yscrollbar.set,
            offset="%d,%d" % (-SLOP, -SLOP),
            scrollregion=(
                -SLOP,
                -SLOP,
                COLUMNS * self.font_width + SLOP,
                self.font_height + SLOP,
            ),
        )
        xscrollbar.config(command=self.canvas.xview)
        yscrollbar.config(command=self.canvas.yview)

    def key(self, event: tkinter.Event) -> None:
        """Handle a keyboard event."""
        # print(event)
        assert self.terminal is not None
        if event.keysym == "F5":
            self.terminal.backend.fast_mode ^= True
        elif event.keysym == "Prior":
            self.canvas.yview_scroll(-1, "pages")
        elif event.keysym == "Next":
            self.canvas.yview_scroll(1, "pages")
        elif event.char:
            if len(event.char) > 1 or ord(event.char) > 0xF000:
                # weird mac tk stuff
                pass
            else:
                self.terminal.backend.write_char(event.char)

    def postchars(self, chars: str):
        """Relay the characters from the backend to the controller."""
        assert self.terminal is not None
        self.terminal.output_chars(chars)

    def draw_char(self, line: int, column: int, char: str):
        """Draw a character on the screen."""
        x = column * self.font_width
        y = line * self.font_height
        # print("drawing char", repr(char), "at", (x, y))
        self.canvas.create_text(
            (x, y), text=char, fill=self.fg, anchor="nw", font=self.font
        )
        # Yes, this creates an object for every character.  Yes, it is
        # disgusting, and gets hideously slow after a few thousand lines of
        # output.  The Tkinter front end is mainly intended for testing.
        if self.max_line < line:
            self.max_line = line

    def lines_screen(self):
        """Return the number of lines per screen.

        This is a dummy value for the Tkinter frontend.
        """
        return self.max_line + 1

    # pylint: disable=unused-argument
    def refresh_screen(self, scroll_base: int, cursor_line: int, cursor_column: int):
        """Tkinter refresh method.

        Mostly just moves the cursor.
        """
        x0 = cursor_column * self.font_width
        y0 = cursor_line * self.font_height
        x1 = x0 + self.font_width
        y1 = y0 + self.font_height
        self.canvas.coords(self.cursor_id, (x0, y0, x1, y1))
        if self.max_line < cursor_line:
            self.max_line = cursor_line
        scr_height = (self.max_line + 1) * self.font_height
        self.canvas.config(
            scrollregion=(
                -SLOP,
                -SLOP,
                COLUMNS * self.font_width + SLOP,
                scr_height + SLOP,
            )
        )
        cy = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        y0 -= SLOP
        y1 += SLOP
        # slop makes these calculations weird and possibly incorrect
        # print('cursor[%s:%s] canvas[%s:%s]' % (y0, y1, cy, cy+height))
        if y0 < cy:
            self.canvas.yview_moveto(y0 / scr_height)
        elif y1 > cy + height:
            self.canvas.yview_moveto((y1 - height + SLOP * 2) / scr_height)

    def reinit(self):
        """Clear everything."""
        self.canvas.delete("all")
        bbox = (0, 0, self.font_width, self.font_height)
        self.cursor_id = self.canvas.create_rectangle(bbox)

    def mainloop(self, terminal: Terminal):
        """Set the frontend's terminal and run the main loop."""
        self.terminal = terminal
        self.root.mainloop()


class PygameFrontend(Frontend):
    """Front-end using pygame for rendering."""

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self, target_surface: pygame.Surface | None = None, lines_per_page: int = 8
    ):
        """Create a frontend.

        target_surface: if provided, the surface pygame should draw to.
        lines_per_page: the number of lines per a surface page.
        """
        self.sounds = PygameSounds()
        pygame.init()
        self.font: pygame.font.Font = self._findfont(22)
        self.font_width, self.font_height = self.font.size("X")
        self.width_pixels: int = COLUMNS * self.font_width
        if target_surface is None:
            pygame.display.set_caption("Terminal")
            dim = self.width_pixels, 22 * self.font_height
            target_surface = pygame.display.set_mode(dim)  # , pygame.RESIZABLE)
            target_surface.fill(background_color())
            pygame.display.update()
        self.page_surfaces: list[pygame.Surface] = []
        self.target_surface: pygame.Surface = target_surface
        self.lines_per_page: int = lines_per_page
        self.char_event_num: int = pygame.USEREVENT + 1
        self.terminal: Terminal | None = None

    def _findfont(self, fontsize: int) -> pygame.font.Font:
        # pygame SysFont doesn't help on Windows, so look for specific files in known locations
        paths: list[str] = []
        if "WINDIR" in os.environ:
            path = os.path.join(os.environ["WINDIR"], "Fonts")
            paths.append(os.path.join(path, "TELE.TTF"))
            paths.append(os.path.join(path, "TELETYPE1945-1985.ttf"))
        if "USERPROFILE" in os.environ:
            path = os.path.join(
                os.environ["USERPROFILE"],
                "AppData",
                "Local",
                "Microsoft",
                "Windows",
                "Fonts",
            )
            paths.append(os.path.join(path, "TELE.TTF"))
            paths.append(os.path.join(path, "TELETYPE1945-1985.ttf"))
        for path in paths:
            try:
                return pygame.font.Font(path, fontsize)
            except FileNotFoundError:
                pass
        return pygame.font.SysFont("Teleprinter,TELETYPE 1945-1985,monospace", fontsize)

    def reinit(self, lines_per_page: int | None = None):
        """Clear and reset all terminal state."""
        self.page_surfaces.clear()
        if lines_per_page:
            self.lines_per_page = lines_per_page

    def lines_screen(self) -> int:
        """Return the number of lines on the screen."""
        return self.target_surface.get_height() // self.font_height

    # def alloc_line(self, line_number):
    #    "Bookkeeping to make sure the cursor line is valid after a linefeed"
    #    # turned out unnecessary here
    #    page_number, page_line = divmod(line_number, self.lines_per_page)
    #    page_surface = alloc_page(page_number)
    #    rect1 = (0, page_line * self.font_height, self.width_pixels, self.font_height)
    #    page_surface.fill(background_color(), rect1)

    def alloc_page(self, i: int) -> pygame.Surface:
        """Return the i'th page surface."""
        while len(self.page_surfaces) <= i:
            page_surface = pygame.Surface(
                (self.width_pixels, self.lines_per_page * self.font_height)
            )
            page_surface.fill(background_color())
            self.page_surfaces.append(page_surface)
        return self.page_surfaces[i]

    def blit_page_to_screen(self, page_number: int, scroll_base: int) -> None:
        """Refresh a single page surface to the screen."""
        line0 = page_number * self.lines_per_page
        line1 = (page_number + 1) * self.lines_per_page
        if line1 < scroll_base:
            return  # page is off top of screen
        if line0 > scroll_base + self.lines_screen():
            return  # page is off bottom of screen
        dest = (0, self.font_height * (line0 - scroll_base))
        area = pygame.Rect(
            0, 0, self.width_pixels, self.lines_per_page * self.font_height
        )
        page_surface = self.page_surfaces[page_number]
        # print("blit page", page_number, dest, area)
        self.target_surface.blit(page_surface, dest, area)

    def draw_cursor(self, phys_line: int, column: int) -> None:
        """Draw the cursor."""
        curs = pygame.Rect(
            self.font_width * column,
            self.font_height * phys_line,
            self.font_width,
            self.font_height,
        )
        pygame.draw.rect(self.target_surface, TEXT_COLOR, curs, 1)

    def refresh_screen(
        self, scroll_base: int, cursor_line: int, cursor_column: int
    ) -> None:
        """Refresh the screen."""
        cursor_phys_line = cursor_line - scroll_base
        for i in range(len(self.page_surfaces)):
            self.blit_page_to_screen(i, scroll_base)
        self.draw_cursor(cursor_phys_line, cursor_column)
        pygame.display.update()
        sys.stdout.flush()

    def draw_char(self, line: int, column: int, char: str) -> None:
        """Draw a character on the page backing."""
        text = self.font.render(char, True, TEXT_COLOR)
        page_number, page_line = divmod(line, self.lines_per_page)
        page_surface = self.alloc_page(page_number)
        page_surface.blit(
            text, (self.font_width * column, self.font_height * page_line)
        )

    def postchars(self, chars: str) -> None:
        """Post message with characters to render."""
        pygame.event.post(pygame.event.Event(self.char_event_num, chars=chars))

    def handle_key(self, event: pygame.event.Event) -> None:
        """Handle a keyboard event."""
        assert self.terminal is not None
        if event.unicode:
            assert isinstance(event.unicode, str)
            self.sounds.keypress(event.unicode)
            self.terminal.backend.write_char(event.unicode)
            pygame.display.update()
        elif event.key == pygame.K_F5:
            self.terminal.backend.fast_mode = True
        elif event.key == pygame.K_F7:
            self.sounds.lid()
        elif event.key == pygame.K_PAGEUP:
            self.sounds.platen()
            self.terminal.page_up()
        elif event.key == pygame.K_PAGEDOWN:
            self.sounds.platen()
            self.terminal.page_down()
        else:
            pass
            # print(event)

    def mainloop(self, terminal: Terminal) -> None:
        """Run game loop."""
        self.terminal = terminal
        self.sounds.start()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.sounds.stop()
                    pygame.mixer.quit()
                    pygame.quit()
                    sys.exit()
                if event.type == pygame.KEYDOWN:
                    self.handle_key(event)
                if event.type == pygame.KEYUP:
                    if event.key == pygame.K_F5:
                        self.terminal.backend.fast_mode = False
                if event.type == pygame.VIDEORESIZE:
                    # Extremely finicky, but it seems to work
                    height = event.dict["size"][1]
                    height = height // self.font_height * self.font_height
                    pygame.display.set_mode(
                        (self.width_pixels, height), pygame.RESIZABLE
                    )
                    self.target_surface.fill(background_color())
                    self.terminal.scroll_into_view()
                    self.terminal.refresh_screen()
                if event.type == self.char_event_num:
                    self.terminal.output_chars(event.chars)
                    self.sounds.print_chars(event.chars)
                if event.type in self.sounds.EVENTS:
                    # Sound events notify that playback is ended on a sound or channel
                    self.sounds.event(event.type)


# pylint: disable=unused-argument,no-self-use
class DummyFrontend(Frontend):
    """Front end that does nothing except a minimal connection to the terminal."""

    def __init__(self, terminal: Terminal | None = None):
        """terminal: the terminal this frontend is using."""
        self.terminal = terminal

    def postchars(self, chars: str) -> None:
        """Display all characters on the terminal."""
        assert self.terminal is not None
        self.terminal.output_chars(chars)

    def draw_char(self, line: int, column: int, char: str) -> None:
        """Draw a character at a given position.

        The Dummy frontend ignores the position and just outputs.
        """
        sys.stdout.write(char)
        sys.stdout.flush()

    def lines_screen(self) -> int:
        """Return the number of lines per screen."""
        return 24

    def refresh_screen(
        self, scroll_base: int, cursor_line: int, cursor_column: int
    ) -> None:
        """Refersh the terminal screen.

        Does nothing for the Dummy frontend.
        """
        pass

    def reinit(self) -> None:
        """Reset the frontend.

        Does nothing for the Dummy.
        """
        pass

    def mainloop(self, terminal: Terminal) -> None:
        """Run the frontend with the given terminal."""
        self.terminal = terminal
        buffer = sys.stdin.buffer
        assert isinstance(buffer, BufferedIOBase)
        while True:
            chars = buffer.read1(1).decode("ascii", "replace")
            if not chars:
                return
            terminal.backend.write_char(chars)


class LoopbackBackend(Backend):
    """Just sends characters from the keyboard back to the screen."""

    def __init__(self, postchars: Callable[[str], None] = lambda chars: None):
        """postchars: function to deal with output characters."""
        self.postchars = postchars

    def write_char(self, char: str) -> None:
        """Echo back keyboard character."""
        self.postchars(char)

    def thread_target(self):
        """Start-up the thread.

        Does nothing for loopback.
        """
        pass


class ParamikoBackend(Backend):
    """Connects a remote host to the terminal."""

    def __init__(
        self,
        host: str,
        username: str,
        keyfile: str,
        port: int = 22,
        postchars: Callable[[str], None] = lambda chars: None,
    ):
        """Create the backend.

        host: host address.
        username: login name.
        keyfile: file path for keys.
        port: host connection port.
        postchars: function to output characters on the frontend.
        """
        self.fast_mode: bool = False
        self.channel: Channel | None = None
        self.postchars = postchars
        self.host: str = host
        self.port: int = port
        self.username: str = username
        self.keyfile: str = keyfile

    def write_char(self, char: str) -> None:
        """Send a keyboard character to the host."""
        if self.channel is not None:
            self.channel.send(char.encode())
        else:
            self.postchars(char)

    def thread_target(self) -> None:
        """Start the thread."""
        ssh = paramiko.Transport((self.host, self.port))
        key = paramiko.RSAKey.from_private_key_file(self.keyfile)
        ssh.connect(username=self.username, pkey=key)
        self.channel = ssh.open_session()
        self.channel.get_pty(term="tty33")
        self.channel.invoke_shell()
        while True:
            if self.fast_mode:
                data = self.channel.recv(1024)
                if not data:
                    break
                self.postchars(data.decode("ascii", "replace"))
            else:
                byte = self.channel.recv(1)
                if not byte:
                    break
                self.postchars(byte.decode("ascii", "replace"))
                time.sleep(0.105)
        self.channel = None
        self.postchars("Disconnected. Local mode.\r\n")


class TelnetBackend(Backend):
    """Connects a remote host to the terminal."""

    def __init__(
        self,
        host: str,
        port: int = 23,
        postchars: Callable[[str], None] = lambda chars: None,
    ):
        """Create the backend.

        host: the host address.
        port: the host connection port.
        postchars: function to output characters on the frontend.
        """
        self.fast_mode: bool = False
        self.conn: Telnet | None = None
        self.postchars = postchars
        self.host = host
        self.port = port
        self.will_naws: int = 0

    def write_char(self, char: str):
        """Send a keyboard character to the host."""
        if self.conn is not None:
            self.conn.write(char.encode())
        else:
            self.postchars(char)

    def thread_target(self):
        """Start the thread."""

        def telnet_callback(sock: socket.socket, cmd: bytes, opt: bytes):
            assert self.conn is not None
            if cmd == SE:
                sbdata = self.conn.read_sb_data()
                logger.info("SE: %s", sbdata)
                if sbdata == TSPEED + ECHO:
                    sock.sendall(IAC + SB + TSPEED + BINARY + b"110,110" + IAC + SE)
                elif sbdata == TTYPE + ECHO:
                    sock.sendall(IAC + SB + TTYPE + BINARY + b"tty33" + IAC + SE)
            if cmd in (DO, DONT):
                if opt in [TTYPE, TSPEED, NAWS]:
                    logger.info("IAC WILL %s", ord(opt))
                    sock.sendall(IAC + WILL + opt)
                    if opt == NAWS:
                        self.will_naws = 1
                else:
                    logger.debug("IAC WONT %s", ord(opt))
                    sock.sendall(IAC + WONT + opt)
            elif cmd in (WILL, WONT):
                if opt in [TTYPE, TSPEED, NAWS]:
                    logger.info("IAC DO %s", ord(opt))
                    sock.sendall(IAC + DO + opt)
                else:
                    logger.debug("IAC DONT %s", ord(opt))
                    sock.sendall(IAC + DONT + opt)

        with Telnet(host=self.host, port=self.port) as self.conn:
            self.conn.set_option_negotiation_callback(telnet_callback)
            while True:
                try:
                    data = self.conn.read_eager()
                except (EOFError, ConnectionResetError):
                    break
                time_now = int(time.time())
                if self.will_naws and time_now > self.will_naws + 30:
                    self.will_naws = time_now
                    self.conn.sock.sendall(
                        IAC + SB + NAWS + bytes([0, 72, 0, 24]) + IAC + SE
                    )
                if not data:
                    continue
                logger.info("%d bytes", len(data))
                for datum in data:
                    try:
                        self.postchars(bytes([datum]).decode("ascii", "replace"))
                    except pygame.error:
                        logger.error("ERR '%c'", datum)
                    if not self.fast_mode:
                        time.sleep(0.105)
        self.conn = None
        self.postchars("Disconnected. Local mode.\r\n")


class TelnetLib3Backend(Backend):
    """A backend using telnetlib3."""

    def __init__(
        self,
        host: str,
        port: int = 23,
        lines_per_screen: int = 24,
        postchars: Callable[[str], None] = lambda chars: None,
    ) -> None:
        """Create the backend.

        host: the host address to connect to.
        port: the host port.
        lines_per_screen: not too important.
        postchars: routine to put characters into the frontend.
        """
        super().__init__(postchars)
        self._host = host
        self._port = port
        self._lines_per_screen = lines_per_screen
        self._reader: telnetlib3.TelnetUnicodeReader | None = None
        self._writer: telnetlib3.TelnetUnicodeWriter | None = None

    def write_char(self, char: str) -> None:
        """Write a character from the keyboard to the backend."""
        if self._writer is not None:
            self._writer.write(char)
        else:
            self.postchars(char)

    async def reader(self) -> None:
        """Read input from the telnet and send it to the frontend in a loop."""
        while self._reader is not None:
            data = await self._reader.read(1)
            try:
                self.postchars(data)
            except pygame.error:
                logger.error(f"ERR {data}")
            if not self.fast_mode:
                await asyncio.sleep(0.105)

    def thread_target(self) -> None:
        """Set everything up."""
        runner = asyncio.Runner()
        self._reader, self._writer = runner.run(
            telnetlib3.open_connection(
                self._host,
                self._port,
                encoding="ascii",
                term="tty33",
                cols=COLUMNS,
                rows=self._lines_per_screen,
                tspeed=(110, 110),
            )
        )
        runner.run(self.reader())


class FiledescBackend(Backend, abc.ABC):
    """Base classes for backends using os.read/write."""

    def __init__(
        self,
        lecho: bool = False,
        crmod: bool = False,
        postchars: Callable[[str], None] = lambda chars: None,
    ):
        r"""Create the class.

        lecho: flag for if we should echo input characters.
        crmod: flag for if we should convert newlines to \r\n.
        """
        self.fast_mode: bool = False
        self.channel = None
        self.postchars = postchars
        self.write_fd: int | None = None
        self.read_fd: int | None = None
        self.crmod = crmod
        self.lecho = lecho

    def write_char(self, char: str) -> None:
        """Place a character into the output."""
        if self.write_fd is not None:
            if self.crmod:
                char = char.replace("\r", "\n")
            os.write(self.write_fd, char.encode())
            if self.lecho:
                if self.crmod:
                    char = char.replace("\n", "\r\n")
                self.postchars(char)
        else:
            self.postchars(char)

    @abc.abstractmethod
    def setup(self) -> None:
        """Initialize the file."""
        raise NotImplementedError

    def teardown(self):
        """End the file."""
        if self.read_fd is not None:
            os.close(self.read_fd)
        if self.write_fd is not None:
            os.close(self.write_fd)
        self.read_fd = self.write_fd = None

    def __enter__(self) -> Self:
        """Run the setup, automatically teardown when finished."""
        self.setup()
        return self

    # pylint:disable=unused-argument
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Teardown when finished."""
        self.teardown()

    def thread_target(self):
        """Start up the thread."""
        with self:
            while True:
                if self.fast_mode:
                    assert self.read_fd is not None
                    data = os.read(self.read_fd, 1024)
                    if not data:
                        break
                    if self.crmod:
                        data = data.replace(b"\n", b"\r\n")
                    self.postchars(data.decode("ascii", "replace"))
                else:
                    try:
                        assert self.read_fd is not None
                        byte = os.read(self.read_fd, 1)
                    except OSError:
                        break
                    if not byte:
                        break
                    if self.crmod:
                        byte = byte.replace(b"\n", b"\r\n")
                    self.postchars(byte.decode("ascii", "replace"))
                    time.sleep(0.105)
        self.postchars("Disconnected. Local mode.\r\n")


class PipeBackend(FiledescBackend):
    """Backend for a subprocess running in a pipe pair.

    Not very useful, but cross-platform.
    """

    def __init__(self, cmd: list[str] | str, shell: bool = False, **kwargs: Any):
        """Create the pipe.

        cmd:command to the pipe shell.
        shell:flag to subprocess.
        """
        super().__init__(**kwargs)
        self.cmd = cmd
        self.shell = shell
        self.proc: subprocess.Popen[bytes] | None = None

    def setup(self):
        """Start the process and hooks up the file descriptors."""
        self.proc = subprocess.Popen(
            self.cmd,
            shell=self.shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.write_fd = self.proc.stdin.fileno()
        self.read_fd = self.proc.stdout.fileno()

    def teardown(self):
        """Close the file descriptors."""
        # Is there a good way to close this other than let gc take care of it?
        self.proc = None
        self.read_fd = self.write_fd = None


class PtyBackend(FiledescBackend):
    """Backend for a subprocess running in a pipe pair.

    Not very useful, but cross-platform.
    """

    def __init__(self, cmd: str | list[str], shell: bool = False, **kwargs: Any):
        """Create the backend.

        cmd: command to run.
        shell: flag for subprocess.
        """
        super().__init__(**kwargs)
        self.cmd = cmd
        if type(cmd) is str:
            if shell:
                self.args: list[str] = ["sh", "-c", cmd]
            else:
                self.args: list[str] = shlex.split(cmd)
        else:
            assert not isinstance(cmd, str)
            self.args: list[str] = cmd

    def setup(self):
        """Start the process and hooks up the file descriptors."""
        assert gotpty
        pid, master = pty.fork()
        if pid:
            self.write_fd = self.read_fd = master
        else:
            try:
                attr = termios.tcgetattr(0)
                attr[3] &= ~(termios.ECHOE | termios.ECHOKE)
                attr[3] |= termios.ECHOPRT | termios.ECHOK
                attr[4] = termios.B110
                attr[5] = termios.B110
                attr[6][termios.VERASE] = b"#"
                attr[6][termios.VKILL] = b"@"
                termios.tcsetattr(0, termios.TCSANOW, attr)
                os.environ["TERM"] = "tty33"
                os.execvp(self.args[0], self.args)
            except Exception as ex:
                os.write(2, str(ex).encode("ascii", "replace"))
                os.write(2, b"\r\n")
                os._exit(126)
            os._exit(126)

    def teardown(self):
        """Close the file descriptor."""
        assert self.read_fd is not None
        os.close(self.read_fd)
        self.read_fd = self.write_fd = None


def main(frontend: Frontend, backend: Backend):
    """Create and execute a terminal."""
    my_term = Terminal(frontend, backend)
    backend.postchars = frontend.postchars
    backend_thread = threading.Thread(target=backend.thread_target)
    backend_thread.start()
    frontend.mainloop(my_term)


# main(PygameFrontend(), PtyBackend("sh"))

# main(PygameFrontend(), PipeBackend('powershell -noexit -command ". mode.com con: cols=72; cd \\; sleep 2"', crmod=True, lecho=False))
# main(PygameFrontend(), TelnetBackend("telehack.com", port=23))
# main(PygameFrontend(), TelnetBackend("bbs.fozztexx.com"))
# main(PygameFrontend(), ParamikoBackend("172.23.97.23", "user", port=2222, keyfile="C:\\Users\\user\\.ssh\\id_rsa"))
# main(TkinterFrontend(), PtyBackend('sh'))
# main(TkinterFrontend(), LoopbackBackend('sh'))

# main(PygameFrontend(), LoopbackBackend())
# main(TkinterFrontend(), ConptyBackend('ubuntu'))
# main(PygameFrontend(), PipeBackend('py -3 -i -c ""', crmod=True, lecho=True))
# main(DummyFrontend(), LoopbackBackend())
# main(DummyFrontend(), PtyBackend('sh'))
# AbstractLine.unit_test('bold\rbold')
# AbstractLine.unit_test('___________\runderlined')
# AbstractLine.unit_test('b\bbo\bol\bld\bd')
# AbstractLine.unit_test('_\bu_\bn_\bd_\be_\br_\bl_\bi_\bn_\be_\bd')
# AbstractLine.unit_test('Tabs\tone\ttwo\tthree\tfour')
# AbstractLine.unit_test('Spaces  one     two     three   four    ')
# AbstractLine.unit_test(
#        'Test\tb\bbo\bol\bod\bd\t'
#        '_\bu_\bn_\bd_\be_\br_\bl_\bi_\bn_\be_\bd\t'
#        'bold\b\b\b\bbold\t'
#        '__________\b\b\b\b\b\b\b\b\b\bunderlined\t'
#        'both\b\b\b\b____\b\b\b\bboth\t'
#        'And here is some junk to run off the right hand edge.')
# AbstractLine.unit_test("Hello, world.  This line has some spaces.")
