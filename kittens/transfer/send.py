#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>


import os
import stat
from collections import deque
from enum import auto
from itertools import count
from time import monotonic
from typing import (
    IO, Callable, Deque, Dict, Iterable, Iterator, List, Optional, Sequence,
    Tuple, Union
)

from kitty.cli_stub import TransferCLIOptions
from kitty.fast_data_types import FILE_TRANSFER_CODE, wcswidth
from kitty.file_transmission import (
    Action, Compression, FileTransmissionCommand, FileType, NameReprEnum,
    TransmissionType, encode_password
)
from kitty.typing import KeyEventType
from kitty.utils import sanitize_control_codes

from ..tui.handler import Handler
from ..tui.loop import Loop, debug
from ..tui.operations import styled, without_line_wrap
from ..tui.spinners import Spinner
from ..tui.utils import human_size
from .utils import (
    IdentityCompressor, ZlibCompressor, abspath, expand_home, home_path,
    random_id, render_progress_in_width, safe_divide,
    should_be_compressed
)

debug


def get_remote_path(local_path: str, remote_base: str) -> str:
    if not remote_base:
        return local_path.replace(os.sep, '/')
    if remote_base.endswith('/'):
        return os.path.join(remote_base, os.path.basename(local_path))
    return remote_base


class FileState(NameReprEnum):
    waiting_for_start = auto()
    waiting_for_data = auto()
    transmitting = auto()
    finished = auto()
    acknowledged = auto()


class File:

    def __init__(
        self, local_path: str, expanded_local_path: str, file_id: int, stat_result: os.stat_result,
        remote_base: str, file_type: FileType, ttype: TransmissionType = TransmissionType.simple
    ) -> None:
        self.ttype = ttype
        self.state = FileState.waiting_for_start
        self.local_path = local_path
        self.display_name = sanitize_control_codes(local_path)
        self.expanded_local_path = expanded_local_path
        self.permissions = stat.S_IMODE(stat_result.st_mode)
        self.mtime = stat_result.st_mtime_ns
        self.file_size = self.bytes_to_transmit = stat_result.st_size
        self.file_hash = stat_result.st_dev, stat_result.st_ino
        self.remote_path = get_remote_path(self.local_path, remote_base)
        self.remote_path = self.remote_path.replace(os.sep, '/')
        self.file_id = hex(file_id)[2:]
        self.hard_link_target = ''
        self.symbolic_link_target = ''
        self.stat_result = stat_result
        self.file_type = file_type
        self.compression = Compression.zlib if (
            self.file_type is FileType.regular and self.file_size > 4096 and should_be_compressed(self.expanded_local_path)
        ) else Compression.none
        self.compression = Compression.zlib
        self.compressor: Union[ZlibCompressor, IdentityCompressor] = ZlibCompressor() if self.compression is Compression.zlib else IdentityCompressor()
        self.remote_final_path = ''
        self.remote_initial_size = -1
        self.err_msg = ''
        self.actual_file: Optional[IO[bytes]] = None
        self.transmitted_bytes = 0
        self.transmit_started_at = self.transmit_ended_at = 0.

    def __repr__(self) -> str:
        return f'File(name={self.display_name}, ft={self.file_type}, state={self.state})'

    def next_chunk(self, sz: int = 1024 * 1024) -> Tuple[bytes, int]:
        if self.file_type is FileType.symlink:
            self.state = FileState.finished
            ans = self.symbolic_link_target.encode('utf-8')
            return ans, len(ans)
        if self.file_type is FileType.link:
            self.state = FileState.finished
            ans = self.hard_link_target.encode('utf-8')
            return ans, len(ans)
        if self.actual_file is None:
            self.actual_file = open(self.expanded_local_path, 'rb')
        chunk = self.actual_file.read(sz)
        uncompressed_sz = len(chunk)
        is_last = not chunk or self.actual_file.tell() >= self.file_size
        cchunk = self.compressor.compress(chunk)
        if is_last and not isinstance(self.compressor, IdentityCompressor):
            cchunk += self.compressor.flush()
        if is_last:
            self.state = FileState.finished
            self.actual_file.close()
            self.actual_file = None
        return cchunk, uncompressed_sz

    def metadata_command(self) -> FileTransmissionCommand:
        return FileTransmissionCommand(
            action=Action.file, compression=self.compression, ftype=self.file_type,
            name=self.remote_path, permissions=self.permissions, mtime=self.mtime,
            file_id=self.file_id,
        )


def process(cli_opts: TransferCLIOptions, paths: Iterable[str], remote_base: str, counter: Iterator[int]) -> Iterator[File]:
    for x in paths:
        expanded = expand_home(x)
        try:
            s = os.stat(expanded, follow_symlinks=False)
        except OSError as err:
            raise SystemExit(f'Failed to stat {x} with error: {err}') from err
        if stat.S_ISDIR(s.st_mode):
            yield File(x, expanded, next(counter), s, remote_base, FileType.directory)
            new_remote_base = remote_base
            if new_remote_base:
                new_remote_base = new_remote_base.rstrip('/') + '/' + os.path.basename(x) + '/'
            else:
                new_remote_base = x.replace(os.sep, '/').rstrip('/') + '/'
            yield from process(cli_opts, [os.path.join(x, y) for y in os.listdir(expanded)], new_remote_base, counter)
        elif stat.S_ISLNK(s.st_mode):
            yield File(x, expanded, next(counter), s, remote_base, FileType.symlink)
        elif stat.S_ISREG(s.st_mode):
            yield File(x, expanded, next(counter), s, remote_base, FileType.regular)


def process_mirrored_files(cli_opts: TransferCLIOptions, args: Sequence[str]) -> Iterator[File]:
    paths = [abspath(x) for x in args]
    try:
        common_path = os.path.commonpath(paths)
    except ValueError:
        common_path = ''
    home = home_path().rstrip(os.sep)
    if common_path and common_path.startswith(home + os.sep):
        paths = [os.path.join('~', os.path.relpath(x, home)) for x in paths]
    yield from process(cli_opts, paths, '', count(1))


def process_normal_files(cli_opts: TransferCLIOptions, args: Sequence[str]) -> Iterator[File]:
    if len(args) < 2:
        raise SystemExit('Must specify at least one local path and one remote path')
    args = list(args)
    remote_base = args.pop().replace(os.sep, '/')
    if len(args) > 1 and not remote_base.endswith('/'):
        remote_base += '/'
    paths = [abspath(x) for x in args]
    yield from process(cli_opts, paths, remote_base, count(1))


def files_for_send(cli_opts: TransferCLIOptions, args: List[str]) -> Tuple[File, ...]:
    if cli_opts.mode == 'mirror':
        files = list(process_mirrored_files(cli_opts, args))
    else:
        files = list(process_normal_files(cli_opts, args))
    groups: Dict[Tuple[int, int], List[File]] = {}

    # detect hard links
    for f in files:
        groups.setdefault(f.file_hash, []).append(f)
    for group in groups.values():
        if len(group) > 1:
            for lf in group[1:]:
                lf.file_type = FileType.link
                lf.hard_link_target = group[0].file_id

    # detect symlinks to other transferred files
    for f in tuple(files):
        if f.file_type is FileType.symlink:
            try:
                link_dest = os.readlink(f.local_path)
            except OSError:
                files.remove(f)
                continue
            f.symbolic_link_target = f'path:{link_dest}'
            q = link_dest if os.path.isabs(link_dest) else os.path.join(os.path.dirname(f.local_path), link_dest)
            try:
                st = os.stat(q)
            except OSError:
                pass
            else:
                fh = st.st_dev, st.st_ino
                if fh in groups:
                    g = tuple(x for x in groups[fh] if os.path.samestat(st, x.stat_result))
                    if g:
                        t = g[0]
                        f.symbolic_link_target = f'fid:{t.file_id}'
    return tuple(files)


class SendState(NameReprEnum):
    waiting_for_permission = auto()
    permission_granted = auto()
    permission_denied = auto()
    canceled = auto()


class Transfer:

    def __init__(self, amt: int = 0):
        self.amt = amt
        self.at = monotonic()

    def is_too_old(self, now: float) -> bool:
        return now - self.at > 30


class ProgressTracker:

    def __init__(self, total_size_of_all_files: int):
        self.total_size_of_all_files = total_size_of_all_files
        self.total_bytes_to_transfer = total_size_of_all_files
        self.active_file: Optional[File] = None
        self.total_transferred = 0
        self.transfers: Deque[Transfer] = deque()
        self.transfered_stats_amt = 0
        self.transfered_stats_interval = 0.
        self.started_at = 0.

    def change_active_file(self, nf: File) -> None:
        now = monotonic()
        self.active_file = nf
        nf.transmit_started_at = now

    def start_transfer(self) -> None:
        self.transfers.append(Transfer())
        self.started_at = monotonic()

    def on_transfer(self, amt: int) -> None:
        if self.active_file is not None:
            self.active_file.transmitted_bytes += amt
        self.total_transferred += amt
        self.transfers.append(Transfer(amt))
        now = self.transfers[-1].at
        while len(self.transfers) > 2 and self.transfers[0].is_too_old(now):
            self.transfers.popleft()
        self.transfered_stats_interval = now - self.transfers[0].at
        self.transfered_stats_amt = sum(t.amt for t in self.transfers)


class SendManager:

    def __init__(self, request_id: str, files: Tuple[File, ...], pw: Optional[str] = None, file_done: Callable[[File], None] = lambda f: None):
        self.files = files
        self.password = encode_password(request_id, pw) if pw else ''
        self.fid_map = {f.file_id: f for f in self.files}
        self.request_id = request_id
        self.state = SendState.waiting_for_permission
        self.all_acknowledged = False
        self.all_started = False
        self.active_idx: Optional[int] = None
        self.current_chunk_uncompressed_sz: Optional[int] = None
        self.prefix = f'\x1b]{FILE_TRANSFER_CODE};id={self.request_id};'
        self.suffix = '\x1b\\'
        self.progress = ProgressTracker(sum(df.file_size for df in self.files if df.file_size >= 0))
        self.file_done = file_done

    @property
    def active_file(self) -> Optional[File]:
        if self.active_idx is not None:
            ans = self.files[self.active_idx]
            if ans.state is FileState.transmitting:
                return ans

    def activate_next_ready_file(self) -> Optional[File]:
        if self.active_idx is not None:
            paf = self.files[self.active_idx]
            paf.transmit_ended_at = monotonic()
            self.file_done(paf)
        for i, f in enumerate(self.files):
            if f.state is FileState.transmitting:
                self.active_idx = i
                self.update_collective_statuses()
                self.progress.change_active_file(f)
                return f
        self.active_idx = None
        self.update_collective_statuses()

    def update_collective_statuses(self) -> None:
        found_not_started = found_not_done = False
        for f in self.files:
            if f.state is not FileState.acknowledged:
                found_not_done = True
            if f.state is FileState.waiting_for_start:
                found_not_started = True
            if found_not_started and found_not_done:
                break
        self.all_acknowledged = not found_not_done
        self.all_started = not found_not_started

    def start_transfer(self) -> str:
        return FileTransmissionCommand(action=Action.send, password=self.password).serialize()

    def next_chunks(self) -> Iterator[str]:
        if self.active_file is None:
            self.activate_next_ready_file()
        af = self.active_file
        if af is None:
            return
        chunk = b''
        self.current_chunk_uncompressed_sz = 0
        while af.state is not FileState.finished and not chunk:
            chunk, usz = af.next_chunk()
            self.current_chunk_uncompressed_sz += usz
        is_last = af.state is FileState.finished
        mv = memoryview(chunk)
        pos = 0
        limit = len(chunk)
        while pos < limit:
            cc = mv[pos:pos + 4096]
            pos += 4096
            final = is_last and pos >= limit
            yield FileTransmissionCommand(action=Action.end_data if final else Action.data, file_id=af.file_id, data=cc).serialize()

    def send_file_metadata(self) -> Iterator[str]:
        for f in self.files:
            yield f.metadata_command().serialize()

    def on_file_status_update(self, ftc: FileTransmissionCommand) -> None:
        file = self.fid_map.get(ftc.file_id)
        if file is None:
            return
        if ftc.status == 'STARTED':
            file.remote_final_path = ftc.name
            file.remote_initial_size = ftc.size
            if file.file_type is FileType.directory:
                file.state = FileState.finished
            else:
                file.state = FileState.waiting_for_data if file.ttype is TransmissionType.rsync else FileState.transmitting
        else:
            if ftc.name and not file.remote_final_path:
                file.remote_final_path = ftc.name
            file.state = FileState.acknowledged
            if ftc.status != 'OK':
                file.err_msg = ftc.status
            if self.active_idx is not None and file is self.files[self.active_idx]:
                self.file_done(self.files[self.active_idx])
                self.active_idx = None
        self.update_collective_statuses()

    def on_file_transfer_response(self, ftc: FileTransmissionCommand) -> None:
        if ftc.action is Action.status:
            if ftc.file_id:
                self.on_file_status_update(ftc)
            else:
                self.state = SendState.permission_granted if ftc.status == 'OK' else SendState.permission_denied


class Send(Handler):
    use_alternate_screen = False

    def __init__(self, cli_opts: TransferCLIOptions, files: Tuple[File, ...]):
        Handler.__init__(self)
        self.manager = SendManager(random_id(), files, cli_opts.permissions_password, self.on_file_done)
        self.cli_opts = cli_opts
        self.transmit_started = False
        self.file_metadata_sent = False
        self.quit_after_write_code: Optional[int] = None
        self.check_paths_printed = False
        names = tuple(x.display_name for x in self.manager.files)
        self.max_name_length = max(6, max(map(wcswidth, names)))
        self.spinner = Spinner()
        self.progress_drawn = True
        self.done_files: List[File] = []
        self.failed_files: List[File] = []

    def send_payload(self, payload: str) -> None:
        self.write(self.manager.prefix)
        self.write(payload)
        self.write(self.manager.suffix)

    def on_file_transfer_response(self, ftc: FileTransmissionCommand) -> None:
        if self.quit_after_write_code is not None:
            return
        if ftc.id != self.manager.request_id:
            return
        if ftc.status == 'CANCELED':
            self.quit_loop(1)
            return
        if self.manager.state is SendState.canceled:
            return
        before = self.manager.state
        self.manager.on_file_transfer_response(ftc)
        if before == SendState.waiting_for_permission:
            if self.manager.state == SendState.permission_denied:
                self.cmd.styled('Permission denied for this transfer', fg='red')
                self.print()
                self.quit_loop(1)
                return
            if self.manager.state == SendState.permission_granted:
                self.cmd.styled('Permission granted for this transfer', fg='green')
                self.print()
                self.send_file_metadata()
        self.asyncio_loop.call_soon(self.loop_tick)

    def start_transfer(self) -> None:
        if self.manager.active_file is None:
            self.manager.activate_next_ready_file()
        if self.manager.active_file is not None:
            self.transmit_started = True
            self.manager.progress.start_transfer()
            self.transmit_next_chunk()
            self.draw_progress()

    def print_check_paths(self) -> None:
        if self.check_paths_printed:
            return
        self.check_paths_printed = True
        self.print('The following file transfers will be performed. A red destination means an existing file will be overwritten.')
        for df in self.manager.files:
            self.cmd.styled(df.file_type.short_text, fg=df.file_type.color)
            self.print(end=' ')
            self.print(df.display_name, '→', end=' ')
            self.cmd.styled(df.remote_final_path, fg='red' if df.remote_initial_size > -1 else None)
            self.print()
        self.print(f'Transferring {len(self.manager.files)} files of total size: {human_size(self.manager.progress.total_bytes_to_transfer)}')
        self.print()
        self.print_continue_msg()

    def print_continue_msg(self) -> None:
        self.print(
            'Press', styled('y', fg='green', bold=True, fg_intense=True), 'to continue or',
            styled('n', fg='red', bold=True, fg_intense=True), 'to abort')

    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        if self.quit_after_write_code is not None:
            return
        if self.check_paths_printed and not self.transmit_started:
            if text.lower() == 'y':
                self.start_transfer()
                return
            if text.lower() == 'n':
                self.abort_transfer()
                self.print('Sending cancel request to terminal')
                return
            self.print_continue_msg()

    def on_key(self, key_event: KeyEventType) -> None:
        if self.quit_after_write_code is not None:
            return
        if key_event.matches('esc'):
            if self.check_paths_printed and not self.transmit_started:
                self.abort_transfer()
                self.print('Sending cancel request to terminal')
            else:
                self.on_interrupt()

    def check_for_transmit_ok(self) -> None:
        if self.manager.state is not SendState.permission_granted:
            return
        if self.cli_opts.confirm_paths:
            if self.manager.all_started:
                self.print_check_paths()
            return
        self.start_transfer()

    def transmit_next_chunk(self) -> None:
        for chunk in self.manager.next_chunks():
            self.send_payload(chunk)
        else:
            if self.manager.all_acknowledged:
                self.transfer_finished()

    def transfer_finished(self) -> None:
        self.send_payload(FileTransmissionCommand(action=Action.finish).serialize())
        self.quit_after_write_code = 1 if self.failed_files else 0

    def on_writing_finished(self) -> None:
        chunk_transmitted = self.manager.current_chunk_uncompressed_sz is not None
        if chunk_transmitted:
            self.manager.progress.on_transfer(self.manager.current_chunk_uncompressed_sz or 0)
            self.manager.current_chunk_uncompressed_sz = None
        if self.quit_after_write_code is not None:
            self.quit_loop(self.quit_after_write_code)
            return
        if self.manager.state is SendState.permission_granted and (not self.transmit_started or chunk_transmitted):
            self.asyncio_loop.call_soon(self.loop_tick)

    def loop_tick(self) -> None:
        if self.manager.state is SendState.waiting_for_permission:
            return
        if self.transmit_started:
            self.transmit_next_chunk()
            self.refresh_progress()
        else:
            self.check_for_transmit_ok()

    def initialize(self) -> None:
        self.send_payload(self.manager.start_transfer())
        if self.cli_opts.permissions_password:
            # dont wait for permission, not needed with a password and
            # avoids a roundtrip
            self.send_file_metadata()
        self.cmd.set_cursor_visible(False)

    def finalize(self) -> None:
        self.cmd.set_cursor_visible(True)

    def send_file_metadata(self) -> None:
        if not self.file_metadata_sent:
            for payload in self.manager.send_file_metadata():
                self.send_payload(payload)
            self.file_metadata_sent = True

    def on_term(self) -> None:
        if self.quit_after_write_code is not None:
            return
        self.cmd.styled('Terminate requested, cancelling transfer, transferred files are in undefined state', fg='red')
        self.print()
        self.abort_transfer(delay=2)

    def on_interrupt(self) -> None:
        if self.quit_after_write_code is not None:
            return
        if self.manager.state is SendState.canceled:
            self.print('Waiting for canceled acknowledgement from terminal, will abort in a few seconds if no response received')
            return
        self.cmd.styled('Interrupt requested, cancelling transfer, transferred files are in undefined state', fg='red')
        self.print()
        self.abort_transfer()

    def abort_transfer(self, delay: float = 5) -> None:
        self.send_payload(FileTransmissionCommand(action=Action.cancel).serialize())
        self.manager.state = SendState.canceled
        self.asyncio_loop.call_later(delay, self.quit_loop, 1)

    def render_progress(
        self, name: str, spinner_char: str = ' ', bytes_so_far: int = 0, total_bytes: int = 0,
        secs_so_far: float = 0., bytes_per_sec: float = 0., is_complete: bool = False
    ) -> None:
        if is_complete:
            bytes_so_far = total_bytes
        self.write(render_progress_in_width(
            name, width=self.screen_size.cols, max_path_length=self.max_name_length, spinner_char=spinner_char,
            bytes_so_far=bytes_so_far, total_bytes=total_bytes, secs_so_far=secs_so_far,
            bytes_per_sec=bytes_per_sec, is_complete=is_complete
        ))

    def erase_progress(self) -> None:
        if self.progress_drawn:
            self.cmd.move_cursor_by(2, 'up')
            self.write('\r')
            self.cmd.clear_to_end_of_screen()
            self.progress_drawn = False

    def on_file_done(self, file: File) -> None:
        self.done_files.append(file)
        if file.err_msg:
            self.failed_files.append(file)
        self.asyncio_loop.call_soon(self.refresh_progress)

    def draw_progress(self) -> None:
        with without_line_wrap(self.write):
            for df in self.done_files:
                sc = styled('✔', fg='green') if not df.err_msg else styled('✘', fg='red')
                self.draw_progress_for_current_file(df, spinner_char=sc, is_complete=True)
                self.print()
            del self.done_files[:]
            is_complete = self.quit_after_write_code is not None
            if is_complete:
                sc = styled('✔', fg='green') if self.quit_after_write_code == 0 else styled('✘', fg='red')
            else:
                sc = self.spinner()
            p = self.manager.progress
            af = self.manager.active_file
            now = monotonic()
            if af is None:
                self.write('─' * self.screen_size.width)
            else:
                self.draw_progress_for_current_file(af, spinner_char=sc)
            self.print()
            self.render_progress(
                'Total', spinner_char=sc,
                bytes_so_far=p.total_transferred, total_bytes=p.total_bytes_to_transfer,
                secs_so_far=now - p.started_at, is_complete=is_complete,
                bytes_per_sec=safe_divide(p.transfered_stats_amt, p.transfered_stats_interval)
            )
            self.print()
        self.asyncio_loop.call_later(self.spinner.interval, self.refresh_progress)
        self.progress_drawn = True

    def refresh_progress(self) -> None:
        self.erase_progress()
        self.draw_progress()

    def draw_progress_for_current_file(self, af: File, spinner_char: str = ' ', is_complete: bool = False) -> None:
        p = self.manager.progress
        now = monotonic()
        self.render_progress(
            af.display_name, spinner_char=spinner_char, is_complete=is_complete,
            bytes_so_far=af.transmitted_bytes, total_bytes=af.bytes_to_transmit,
            secs_so_far=(af.transmit_ended_at or now) - af.transmit_started_at,
            bytes_per_sec=safe_divide(p.transfered_stats_amt, p.transfered_stats_interval)
        )


def send_main(cli_opts: TransferCLIOptions, args: List[str]) -> None:
    print('Scanning files…')
    files = files_for_send(cli_opts, args)
    print(f'Found {len(files)} files and directories, requesting transfer permission…')
    loop = Loop()
    handler = Send(cli_opts, files)
    loop.loop(handler)
    if handler.failed_files:
        print(f'Transfer of {len(handler.failed_files)} out of {len(handler.manager.files)} files failed')
        for ff in handler.failed_files:
            print(styled(ff.display_name, fg='red'))
            print(' ', ff.err_msg)

    raise SystemExit(loop.return_code)