# SPDX-License-Identifier: GPL-3.0-or-later

# Copyright 2019-2022 igo95862

# This file is part of bubblejail.
# bubblejail is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# bubblejail is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with bubblejail.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from asyncio import (
    CancelledError,
    Future,
    create_subprocess_exec,
    get_running_loop,
    open_unix_connection,
    wait_for,
)
from asyncio.subprocess import Process
from functools import cached_property
from os import O_CLOEXEC, O_NONBLOCK, environ, kill, pipe2, stat
from pathlib import Path
from signal import SIGTERM
from tempfile import TemporaryDirectory, TemporaryFile
from typing import IO, Any, List, Optional, Set, Type, TypedDict, cast

from tomli import loads as toml_loads
from tomli_w import dump as toml_dump
from xdg.BaseDirectory import get_runtime_dir

from .bubblejail_helper import RequestRun
from .bubblejail_seccomp import SeccompState
from .bubblejail_utils import (
    FILE_NAME_METADATA,
    FILE_NAME_SERVICES,
    BubblejailSettings,
)
from .bubblejail_home_plugins import HOME_PLUGINS, HomeDirectoryPlugin
from .bwrap_config import (
    Bind,
    BwrapConfigBase,
    DbusSessionArgs,
    DbusSystemArgs,
    FileTransfer,
    LaunchArguments,
    SeccompDirective,
)
from .exceptions import BubblejailException, BubblewrapRunError
from .services import ServiceContainer as BubblejailInstanceConfig
from .services import (
    ServicesConfDictType,
    ServiceWantsDbusSessionBind,
    ServiceWantsHomeBind,
)


def sigterm_bubblejail_handler(bwrap_pid: int) -> None:
    with open(f"/proc/{bwrap_pid}/task/{bwrap_pid}/children") as child_file:
        # HACK: assuming first child of the first task is the bubblejail-helper
        helper_pid = int(child_file.read().split()[0])

    kill(helper_pid, SIGTERM)
    # No need to wait as the bwrap should terminate when helper exits


def copy_data_to_temp_file(data: bytes) -> IO[bytes]:
    temp_file = TemporaryFile()
    temp_file.write(data)
    temp_file.seek(0)
    return temp_file


class ConfDict(TypedDict, total=False):
    service: ServicesConfDictType
    services: List[str]
    executable_name: List[str]
    share_local_time: bool
    filter_disk_sync: bool


class BubblejailInstance:

    def __init__(self, instance_home: Path):
        self.name = instance_home.stem
        # Instance directory located at $XDG_DATA_HOME/bubblejail/
        self.instance_directory = instance_home
        # If instance directory does not exists we can't do much
        # Probably someone used 'run' command before 'create'
        if not self.instance_directory.exists():
            raise BubblejailException("Instance directory does not exist")

    # region Paths
    @cached_property
    def runtime_dir(self) -> Path:
        return Path(get_runtime_dir() + f'/bubblejail/{self.name}')

    @cached_property
    def path_config_file(self) -> Path:
        return self.instance_directory / FILE_NAME_SERVICES

    @cached_property
    def path_metadata_file(self) -> Path:
        return self.instance_directory / FILE_NAME_METADATA

    @cached_property
    def path_home_directory(self) -> Path:
        return self.instance_directory / 'home'

    @cached_property
    def path_home_plugins_directory(self) -> Path:
        return self.instance_directory / 'home_plugins'

    @cached_property
    def path_runtime_helper_dir(self) -> Path:
        """Helper run-time directory"""
        return self.runtime_dir / 'helper'

    @cached_property
    def path_runtime_helper_socket(self) -> Path:
        return self.path_runtime_helper_dir / 'helper.socket'

    @cached_property
    def path_runtime_dbus_session_socket(self) -> Path:
        return self.runtime_dir / 'dbus_session_proxy'

    @cached_property
    def path_runtime_dbus_system_socket(self) -> Path:
        return self.runtime_dir / 'dbus_system_proxy'

    # endregion Paths

    # region Metadata

    def _get_metadata_dict(self) -> dict[str, Any]:
        try:
            with open(self.path_metadata_file) as metadata_file:
                return toml_loads(metadata_file.read())
        except FileNotFoundError:
            return {}

    def _save_metadata_key(self, key: str, value: Any) -> None:
        toml_dict = self._get_metadata_dict()
        toml_dict[key] = value

        with open(self.path_metadata_file, mode='wb') as metadata_file:
            toml_dump(toml_dict, metadata_file)

    def _get_metadata_value(self, key: str) -> Optional[str]:
        try:
            value = self._get_metadata_dict()[key]
            if isinstance(value, str):
                return value
            else:
                raise TypeError(f"Expected str, got {value}")
        except KeyError:
            return None

    @property
    def metadata_creation_profile_name(self) -> Optional[str]:
        return self._get_metadata_value('creation_profile_name')

    @metadata_creation_profile_name.setter
    def metadata_creation_profile_name(self, profile_name: str) -> None:
        self._save_metadata_key(
            key='creation_profile_name',
            value=profile_name,
        )

    @property
    def metadata_desktop_entry_name(self) -> Optional[str]:
        return self._get_metadata_value('desktop_entry_name')

    @metadata_desktop_entry_name.setter
    def metadata_desktop_entry_name(self, desktop_entry_name: str) -> None:
        self._save_metadata_key(
            key='desktop_entry_name',
            value=desktop_entry_name,
        )

    # endregion Metadata

    def _read_config_file(self) -> str:
        with (self.path_config_file).open() as f:
            return f.read()

    def _read_config(
            self,
            config_contents: Optional[str] = None) -> BubblejailInstanceConfig:

        if config_contents is None:
            config_contents = self._read_config_file()

        conf_dict = cast(ServicesConfDictType, toml_loads(config_contents))

        return BubblejailInstanceConfig(conf_dict)

    def save_config(self, config: BubblejailInstanceConfig) -> None:
        with open(self.path_config_file, mode='wb') as conf_file:
            toml_dump(config.get_service_conf_dict(), conf_file)

    async def send_run_rpc(
        self,
        args_to_run: List[str],
        wait_for_response: bool = False,
    ) -> Optional[str]:
        (reader, writer) = await open_unix_connection(
            path=self.path_runtime_helper_socket,
        )

        request = RequestRun(
            args_to_run=args_to_run,
            wait_response=wait_for_response,
        )
        writer.write(request.to_json_byte_line())
        await writer.drain()

        try:
            if wait_for_response:
                data: Optional[str] \
                    = request.decode_response(
                        await wait_for(
                            fut=reader.readline(),
                            timeout=3,
                        )
                )
            else:
                data = None
        finally:
            writer.close()
            await writer.wait_closed()

        return data

    def is_running(self) -> bool:
        return self.path_runtime_helper_socket.is_socket()

    async def async_run_init(
        self,
        args_to_run: List[str],
        debug_shell: bool = False,
        dry_run: bool = False,
        debug_helper_script: Optional[Path] = None,
        debug_log_dbus: bool = False,
        extra_bwrap_args: Optional[List[str]] = None,
    ) -> None:

        instance_config = self._read_config()

        # Create init
        init = BubblejailInit(
            parent=self,
            instance_config=instance_config,
            is_shell_debug=debug_shell,
            is_helper_debug=debug_helper_script is not None,
            is_log_dbus=debug_log_dbus,
        )

        async with init:
            bwrap_args = ['/usr/bin/bwrap']
            # Pass option args file descriptor
            bwrap_args.append('--args')
            bwrap_args.append(str(init.get_args_file_descriptor()))

            # Append extra args
            if extra_bwrap_args is not None:
                bwrap_args.extend(extra_bwrap_args)

            # Append command to bwrap depending on debug helper
            if debug_helper_script is not None:
                bwrap_args.extend(('python', '-X', 'dev', '-c'))
                with open(debug_helper_script) as f:
                    script_text = f.read()

                bwrap_args.append(script_text)
            else:
                bwrap_args.append(BubblejailSettings.HELPER_PATH_STR)

            if debug_shell:
                bwrap_args.append('--shell')

            if not args_to_run:
                bwrap_args.extend(init.executable_args)
            else:
                bwrap_args.extend(args_to_run)

            if dry_run:
                print('Bwrap options: ')
                print(' '.join(init.bwrap_options_args))

                print('Bwrap args: ')
                print(' '.join(bwrap_args))

                print('Dbus session args')
                print(' '.join(init.dbus_proxy_args))

                return

            bwrap_process = await create_subprocess_exec(
                *bwrap_args,
                pass_fds=init.file_descriptors_to_pass,
            )
            if __debug__:
                print(f"Bubblewrap started. PID: {repr(bwrap_process)}")

            task_bwrap_main = bwrap_process.wait()

            loop = get_running_loop()
            loop.add_signal_handler(SIGTERM, sigterm_bubblejail_handler,
                                    bwrap_process.pid)

            try:
                await task_bwrap_main
            except CancelledError:
                print('Bwrap cancelled')

            if bwrap_process.returncode != 0:
                raise BubblewrapRunError((
                    "Bubblewrap failed. "
                    "Try running bubblejail in terminal to see the "
                    "exact error."
                ))

            if __debug__:
                print("Bubblewrap terminated")

    async def edit_config_in_editor(self) -> None:
        # Create temporary directory
        with TemporaryDirectory() as tempdir:
            # Create path to temporary file and write exists config
            temp_file_path = Path(tempdir + 'temp.toml')
            with open(temp_file_path, mode='w') as tempfile:
                tempfile.write(self._read_config_file())

            initial_modification_time = stat(temp_file_path).st_mtime
            # Launch EDITOR on the temporary file
            run_args = [environ['EDITOR'], str(temp_file_path)]
            p = await create_subprocess_exec(*run_args)
            await p.wait()

            # If file was not modified do nothing
            if initial_modification_time >= stat(temp_file_path).st_mtime:
                print('File not modified. Not overwriting config')
                return

            # Verify that the new config is valid and save to variable
            with open(temp_file_path) as tempfile:
                new_config_toml = tempfile.read()
                BubblejailInstanceConfig(
                    cast(ServicesConfDictType, toml_loads(new_config_toml))
                )
            # Write to instance config file
            with open(self.path_config_file, mode='w') as conf_file:
                conf_file.write(new_config_toml)


class BubblejailInit:
    def __init__(
        self,
        parent: BubblejailInstance,
        instance_config: BubblejailInstanceConfig,
        is_shell_debug: bool = False,
        is_helper_debug: bool = False,
        is_log_dbus: bool = False,
    ) -> None:
        self.home_bind_path = parent.path_home_directory
        self.runtime_dir = parent.runtime_dir
        # Prevent our temporary file from being garbage collected
        self.temp_files: List[IO[bytes]] = []
        self.file_descriptors_to_pass: List[int] = []
        # Helper
        self.helper_runtime_dir = parent.path_runtime_helper_dir
        self.helper_socket_path = parent.path_runtime_helper_socket

        # Args to dbus proxy
        self.dbus_proxy_args: List[str] = []
        self.dbus_proxy_process: Optional[Process] = None

        self.dbus_proxy_pipe_read: int = -1
        self.dbus_proxy_pipe_write: int = -1

        # Dbus session socket
        self.dbus_session_socket_path = parent.path_runtime_dbus_session_socket

        # Dbus system socket
        self.dbus_system_socket_path = parent.path_runtime_dbus_system_socket

        # Args to bwrap
        self.bwrap_options_args: List[str] = []
        # Debug mode
        self.is_helper_debug = is_helper_debug
        self.is_shell_debug = is_shell_debug
        self.is_log_dbus = is_log_dbus
        # Instance config
        self.instance_config = instance_config

        # Executable args
        self.executable_args: List[str] = []

        self.plugins_dir = parent.path_home_plugins_directory
        self.activated_home_plugins: List[HomeDirectoryPlugin] = []

    def genetate_args(self) -> None:
        # TODO: Reorganize the order to allow for
        # better binding multiple resources in same filesystem path

        dbus_session_opts: Set[str] = set()
        dbus_system_opts: Set[str] = set()
        seccomp_state: Optional[SeccompState] = None
        # Unshare all
        self.bwrap_options_args.append('--unshare-all')
        # Die with parent
        self.bwrap_options_args.append('--die-with-parent')
        # We have our own reaper
        self.bwrap_options_args.append('--as-pid-1')

        if not self.is_shell_debug:
            # Set new session
            self.bwrap_options_args.append('--new-session')

        # Proc
        self.bwrap_options_args.extend(('--proc', '/proc'))
        # Devtmpfs
        self.bwrap_options_args.extend(('--dev', '/dev'))

        # Unset all variables
        self.bwrap_options_args.append('--clearenv')

        for service in self.instance_config.iter_services():
            config_iterator = service.__iter__()

            while True:
                try:
                    config = next(config_iterator)
                except StopIteration:
                    break

                # When we need to send something to generator
                if isinstance(config, ServiceWantsHomeBind):
                    config = config_iterator.send(self.home_bind_path)
                elif isinstance(config, ServiceWantsDbusSessionBind):
                    config = config_iterator.send(
                        self.dbus_session_socket_path)

                if isinstance(config, BwrapConfigBase):
                    self.bwrap_options_args.extend(config.to_args())
                elif isinstance(config, FileTransfer):
                    # Copy files
                    temp_f = copy_data_to_temp_file(config.content)
                    self.temp_files.append(temp_f)
                    temp_file_descriptor = temp_f.fileno()
                    self.file_descriptors_to_pass.append(
                        temp_file_descriptor)
                    self.bwrap_options_args.extend(
                        ('--file', str(temp_file_descriptor), config.dest))
                elif isinstance(config, DbusSessionArgs):
                    dbus_session_opts.add(config.to_args())
                elif isinstance(config, DbusSystemArgs):
                    dbus_system_opts.add(config.to_args())
                elif isinstance(config, SeccompDirective):
                    if seccomp_state is None:
                        seccomp_state = SeccompState()

                    seccomp_state.add_directive(config)
                elif isinstance(config, LaunchArguments):
                    # TODO: implement priority
                    self.executable_args.extend(config.launch_args)
                else:
                    raise TypeError('Unknown bwrap config.')

        if seccomp_state is not None:
            if __debug__:
                seccomp_state.print()

            seccomp_temp_file = seccomp_state.export_to_temp_file()
            seccomp_fd = seccomp_temp_file.fileno()
            self.file_descriptors_to_pass.append(seccomp_fd)
            self.temp_files.append(seccomp_temp_file)
            self.bwrap_options_args.extend(('--seccomp', str(seccomp_fd)))

        # region dbus
        # Session dbus
        self.dbus_proxy_args.extend((
            'xdg-dbus-proxy',
            environ['DBUS_SESSION_BUS_ADDRESS'],
            str(self.dbus_session_socket_path),
        ))

        self.dbus_proxy_pipe_read, self.dbus_proxy_pipe_write \
            = pipe2(O_NONBLOCK | O_CLOEXEC)

        self.dbus_proxy_args.append(f"--fd={self.dbus_proxy_pipe_write}")

        self.dbus_proxy_args.extend(dbus_session_opts)
        self.dbus_proxy_args.append('--filter')
        if self.is_log_dbus:
            self.dbus_proxy_args.append('--log')

        # System dbus
        self.dbus_proxy_args.extend((
            'unix:path=/run/dbus/system_bus_socket',
            str(self.dbus_system_socket_path),
        ))

        self.dbus_proxy_args.append('--filter')
        if self.is_log_dbus:
            self.dbus_proxy_args.append('--log')

        # Bind twice, in /var and /run
        self.bwrap_options_args.extend(
            Bind(
                str(self.dbus_system_socket_path),
                '/var/run/dbus/system_bus_socket').to_args()
        )

        self.bwrap_options_args.extend(
            Bind(
                str(self.dbus_system_socket_path),
                '/run/dbus/system_bus_socket').to_args()
        )
        # endregion dbus

        # Bind helper directory
        self.bwrap_options_args.extend(
            Bind(str(self.helper_runtime_dir), '/run/bubblehelp').to_args())

    def get_args_file_descriptor(self) -> int:
        options_null = '\0'.join(self.bwrap_options_args)

        args_tempfile = copy_data_to_temp_file(options_null.encode())
        args_tempfile_fileno = args_tempfile.fileno()
        self.file_descriptors_to_pass.append(args_tempfile_fileno)
        self.temp_files.append(args_tempfile)

        return args_tempfile_fileno

    async def __aenter__(self) -> None:
        try:
            plugin_paths = tuple(self.plugins_dir.iterdir())
        except FileNotFoundError:
            ...
        else:
            for plugin_path in plugin_paths:
                plugin_name = plugin_path.name
                plugin = HOME_PLUGINS[plugin_name](
                    self.home_bind_path, plugin_path)

                plugin.enter()

                self.activated_home_plugins.append(plugin)

        # Generate args
        self.genetate_args()

        # Create runtime dir
        # If the dir exists exception will be raised indicating that
        # instance is already running or did not clean-up properly.
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        # Create helper directory
        self.helper_runtime_dir.mkdir(mode=0o700)

        # Dbus session proxy
        running_loop = get_running_loop()
        dbus_proxy_ready_future: Future[bool] = Future()

        def proxy_ready_callback() -> None:
            try:
                with open(self.dbus_proxy_pipe_read, closefd=False) as f:
                    f.read()
            except Exception as e:
                dbus_proxy_ready_future.set_exception(e)
            else:
                dbus_proxy_ready_future.set_result(True)

            running_loop.remove_reader(self.dbus_proxy_pipe_read)

        running_loop.add_reader(
            self.dbus_proxy_pipe_read,
            proxy_ready_callback,
        )

        # Pylint does not recognize *args for some reason
        # pylint: disable=E1120
        self.dbus_proxy_process = await create_subprocess_exec(
            *self.dbus_proxy_args,
            pass_fds=[self.dbus_proxy_pipe_write],
        )

        await wait_for(dbus_proxy_ready_future, timeout=1)

        if self.dbus_proxy_process.returncode is not None:
            raise ValueError(
                f"dbus proxy error code: {self.dbus_proxy_process.returncode}")

    async def __aexit__(
        self,
        exc_type: Type[BaseException],
        exc: BaseException,
        traceback: Any,  # ???: What type is traceback
    ) -> None:
        # Cleanup
        try:
            if self.dbus_proxy_process is not None:
                self.dbus_proxy_process.terminate()
                await self.dbus_proxy_process.wait()
        except ProcessLookupError:
            ...

        for t in self.temp_files:
            t.close()

        try:
            self.helper_socket_path.unlink()
        except FileNotFoundError:
            ...

        try:
            self.helper_runtime_dir.rmdir()
        except FileNotFoundError:
            ...
        except OSError:
            ...

        try:
            self.dbus_session_socket_path.unlink()
        except FileNotFoundError:
            ...

        try:
            self.dbus_system_socket_path.unlink()
        except FileNotFoundError:
            ...

        try:
            self.runtime_dir.rmdir()
        except FileNotFoundError:
            ...
        except OSError:
            ...

        for plugin in self.activated_home_plugins:
            plugin.exit()


class BubblejailProfile:
    def __init__(
        self,
        dot_desktop_path: Optional[str] = None,
        is_gtk_application: bool = False,
        services:  Optional[ServicesConfDictType] = None,
        description: str = 'No description',
        import_tips: str = 'None',
    ) -> None:
        self.dot_desktop_path = (Path(dot_desktop_path)
                                 if dot_desktop_path is not None else None)
        self.is_gtk_application = is_gtk_application
        self.config = BubblejailInstanceConfig(services)
        self.description = description
        self.import_tips = import_tips


class BubblejailInstanceMetadata:
    def __init__(
        self,
        parent: BubblejailInstance,
        creation_profile_name: Optional[str] = None,
        desktop_entry_name: Optional[str] = None,
    ):
        self.parent = parent
        self.creation_profile_name = creation_profile_name
        self.desktop_entry_name = desktop_entry_name
