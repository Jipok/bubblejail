from subprocess import Popen
from typing import List, IO
from os import environ
from sys import argv
from tempfile import TemporaryFile
from bubblejail_module import DEFAULT_CONFIG, BwrapArgs


def copy_data_to_temp_file(data: bytes) -> IO[bytes]:
    temp_file = TemporaryFile()
    temp_file.write(data)
    temp_file.seek(0)
    return temp_file


def run_bwrap(args_to_target: List[str],
              bwrap_config: BwrapArgs = DEFAULT_CONFIG) -> 'Popen[bytes]':
    bwrap_args: List[str] = ['bwrap']

    for bind_entity in bwrap_config.binds:
        bwrap_args.extend(bind_entity.to_args())

    for ro_entity in bwrap_config.read_only_binds:
        bwrap_args.extend(ro_entity.to_args())

    for dir_entity in bwrap_config.dir_create:
        bwrap_args.extend(dir_entity.to_args())

    for symlink in bwrap_config.symlinks:
        bwrap_args.extend(symlink.to_args())

    # Proc
    bwrap_args.extend(('--proc', '/proc'))
    # Devtmpfs
    bwrap_args.extend(('--dev', '/dev'))
    # Unshare all
    bwrap_args.append('--unshare-all')
    # Die with parent
    bwrap_args.append('--die-with-parent')

    if bwrap_config.share_network:
        bwrap_args.append('--share-net')

    # Copy files
    # Prevent our temporary file from being garbage collected
    temp_files: List[IO[bytes]] = []
    file_descriptors_to_pass: List[int] = []
    for f in bwrap_config.files:
        temp_f = copy_data_to_temp_file(f.content)
        temp_files.append(temp_f)
        temp_file_descriptor = temp_f.fileno()
        file_descriptors_to_pass.append(temp_file_descriptor)
        bwrap_args.extend(('--file', str(temp_file_descriptor), f.dest))

    # Unset all variables
    for e in environ:
        if e not in bwrap_config.env_no_unset:
            bwrap_args.extend(('--unsetenv', e))

    # Set enviromental variables
    for env_var in bwrap_config.enviromental_variables:
        bwrap_args.extend(env_var.to_args())

    # Change directory
    bwrap_args.extend(('--chdir', '/home/user'))

    bwrap_args.extend(args_to_target)
    p = Popen(bwrap_args, pass_fds=file_descriptors_to_pass)
    p.wait()
    return p


if __name__ == "__main__":
    run_bwrap(argv[1:])