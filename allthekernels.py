"""All the Kernels: all your kernels in one kernel.

Like magic!
"""

import os
import sys
import logging

from tornado.ioloop import IOLoop

import zmq
from zmq.eventloop import ioloop
ioloop.install()
from zmq.eventloop.future import Context

from traitlets import Dict

from jupyter_client import KernelManager
from jupyter_client.channels import ZMQSocketChannel
from ipykernel.kernelbase import Kernel
from ipykernel.kernelapp import IPKernelApp

from queue import Empty


banner = """\
All The Kernels: A single Jupyter kernel that multiplexes.

Per default, all cells will be executed in the default python kernel. If the
first line of a cell starts with `>`, the line will be parsed as kernel name and
the rest of the cell will be executed in that kernel.

For instance,

    >python2
    def foo():
        ...

will run the cell in a Python 2 kernel, and

    >julia-0.4

will run in Julia 0.4, etc.

You can also set a new default kernel by prefixing the kernel name with `!`:

    >!ir

In this case the current cell and all further cells without a kernel name will
be executed in an R kernel.
"""


__version__ = '1.2.0.dev'


class KernelProxy(object):
    """A proxy for a single kernel


    Hooks up relay of messages on the shell channel.
    """
    def __init__(self, manager, shell_upstream, session):
        self.manager = manager
        self.shell = self.manager.connect_shell()
        self.shell_channel = ZMQSocketChannel(self.shell, session)
        self.shell_upstream = shell_upstream
        self.iopub_url = self.manager._make_url('iopub')

    async def relay_shell(self):
        """Coroutine for relaying any shell replies"""
        while True:
            msg = await self.shell.recv_multipart()
            self.shell_upstream.send_multipart(msg)


class AllTheKernels(Kernel):
    """Kernel class for proxying ALL THE KERNELS YOU HAVE"""
    implementation = 'AllTheKernels'
    implementation_version = __version__
    language_info = {
        'name': 'all-of-them',
        'mimetype': 'text/plain',
    }
    banner = banner

    kernels = Dict()
    default_kernel = os.environ.get('ATK_DEFAULT_KERNEL') or 'python%i' % (sys.version_info[0])
    _atk_parent = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.future_context = ctx = Context()
        self.iosub = ctx.socket(zmq.SUB)
        self.iosub_starting = ctx.socket(zmq.SUB)
        self.iosub_starting_channel = ZMQSocketChannel(self.iosub_starting, self.session)
        self.iosub.subscribe = b''
        self.shell_stream = self.shell_streams[0]
        logging.warning('==============================ATK instanciated')

    def start(self):
        super().start()
        loop = IOLoop.current()
        loop.add_callback(self.relay_iopub_messages)
        logging.warning('======================ATK started')

    async def relay_iopub_messages(self):
        """Coroutine for relaying IOPub messages from all of our kernels"""
        while True:
            msg = await self.iosub.recv_multipart()
            self.iopub_socket.send_multipart(msg)

    async def start_kernel(self, name):
        """Start a new kernel"""
        logging.warning('entering start kernel==============================')
        base, ext = os.path.splitext(self.parent.connection_file)
        cf = '{base}-{name}{ext}'.format(
            base=base,
            name=name,
            ext=ext,
        )
        manager = KernelManager(
            kernel_name=name,
            session=self.session,
            context=self.future_context,
            connection_file=cf,
        )
        manager.start_kernel()
        self.kernels[name] = kernel = KernelProxy(
            manager=manager,
            shell_upstream=self.shell_stream,
            session=self.session)

        self.iosub_starting.connect(kernel.iopub_url)
       #--------------------------------------------------------
        logging.warning('start kernel before ensure ==============================')
        await self.ensure_kernel_connection(kernel)
        self.iosub_starting.disconnect(kernel.iopub_url)
        self.iosub.connect(kernel.iopub_url)
        IOLoop.current().add_callback(kernel.relay_shell)

        #--------------------------------------------------------
        return self.kernels[name]

    async def ensure_kernel_connection(self, kernel):
        while True:
            logging.warning('sending kernel_info====================')
            #kernel.kernel_info()
            info_msg = self.session.msg("kernel_info_request")
            kernel.shell_channel.send(info_msg)
            #self.session.send(kernel.shell, info_msg, ident=kernel.shell.get(zmq.IDENTITY))
            try:
                logging.warning('waiting for kernel_info_reply')
                msg = await kernel.shell_channel.get_msg(timeout=1)
            except Empty:
                logging.warning('did not receive kernel_info_reply')
                pass
            else:
                logging.warning('received kenel_info_reply')
                if msg['msg_type'] == 'kernel_info_reply':
                #Checking that IOPub is connected. If it is not connected, start over.
                    try:
                        logging.warning('waiting for status message')
                        await self.iosub_starting_channel.get_msg(timeout=0.2)
                    except Empty:
                        logging.warning('did not receive status message')
                        pass
                    else:
                        logging.warning('Received status message')
                        self._handle_kernel_info_reply(msg)
                        logging.warning('Handled kernel_info_message')
                    break

        while True:
            try:
                msg = await self.iosub_starting_channel.get_msg(timeout=0.2)
            except Empty:
                break

    async def get_kernel(self, name):
        """Get a kernel, start it if it doesn't exist"""
        if name not in self.kernels:
            await self.start_kernel(name)
        return self.kernels[name]

    def set_parent(self, ident, parent, channel="shell"):
        # record the parent message
        self._atk_parent = parent
        return super().set_parent(ident, parent, channel)

    def split_cell(self, cell):
        """Return the kernel name and remaining cell contents

        If no kernel name is specified, use the default kernel.
        """
        if not cell.startswith('>'):
            # no kernel magic, use default kernel
            return self.default_kernel, cell
        split = cell.split('\n', 1)
        if len(split) == 2:
            first_line, cell = split
        else:
            first_line = cell
            cell = ''
        kernel_name = first_line[1:].strip()
        if kernel_name[0] == "!":
            # >!kernelname sets it as the new default
            kernel_name = kernel_name[1:].strip()
            self.default_kernel = kernel_name
        return kernel_name, cell

    def _publish_status(self, status, channel, parent=None):
        """Disabling publishing status messages for relayed

        Status messages will be relayed from the actual kernels.
        """
        if self._atk_parent and self._atk_parent['header']['msg_type'] in {
            'execute_request', 'inspect_request', 'complete_request'
        }:
            self.log.debug("suppressing %s status message.", status)
            return
        else:
            return super()._publish_status(status, channel, parent)

    async def relay_to_kernel(self, stream, ident, parent):
        """Relay a message to a kernel

        Gets the `>kernel` line off of the cell,
        finds the kernel (starts it if necessary),
        then relays the request.
        """
        logging.warning('===================================relay_to_kernel')
        content = parent['content']
        cell = content['code']
        kernel_name, cell = self.split_cell(cell)
        content['code'] = cell
        logging.warning('===================================relay_to_kernel before get_kernel')
        kernel = await self.get_kernel(kernel_name)
        self.log.debug("Relaying %s to %s", parent['header']['msg_type'], kernel_name)
        self.session.send(kernel.shell, parent, ident=ident)

    #execute_request = relay_to_kernel
    async def execute_request(self, stream, ident, parent):
        return await self.relay_to_kernel(stream, ident, parent)

    inspect_request = relay_to_kernel
    complete_request = relay_to_kernel

    def do_shutdown(self, restart):
        for kernel in self.kernels.values():
            kernel.manager.shutdown_kernel(False, restart)
        return super().do_shutdown(restart)


class AllTheKernelsApp(IPKernelApp):

    kernel_class = AllTheKernels
    # disable IO capture
    outstream_class = None

    def _log_level_default(self):
        return 10


main = AllTheKernelsApp.launch_instance


if __name__ == '__main__':
    logging.warning("===== STARTING ATK =======")
    main()
