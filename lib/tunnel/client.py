import asyncio
import warnings
import threading

from lib.tunnel.util import *

from .result import PacketResult
from .result import FuturePacketResult
from .handshake import Handshake
from .protocol import TunnelProtocol


class TunnelBaseClient:
    """
    Implements TunnelProtocol for devices

    Methods
    -------
    start():
        Call this after initialization to start connection
    flush():
        Flush buffer of all data
    async update():
        Call this in a loop to read all buffered data and run callbacks
    register_callback(category, callback):
        Pass a function reference. When a matching category is received, all registered callbacks will receive the data
    write(category, *args):
        Write data to device as a TunnelProtocol packet
    write_handshake(category, *args, write_interval=0.0, timeout=1.0):
        Write data to device as a TunnelProtocol packet. Raise an exception in the call to update()
        if a confirming packet isn't received within the specified timeout
    async packet_callback(result):
        Override this method in a subclass. This gets called when any new correctly parsed packet arrives.
    stop():
        Gracefully shutdown the device connection
    """

    def __init__(self, max_packet_len=128, debug=False):
        self.debug = debug
        self.protocol = TunnelProtocol(max_packet_len, debug)  # defines the tunnel protocol and parse packet behavior

        self.buffer = b''  # stores unparsed characters for the next round

        # when write_handshake is called, an object is stored here that keeps track of its status
        self.pending_handshakes = []

        # when get is called, an object is stored here that keeps track of its status
        self.pending_gets = []

        # a lock to prevent multiple sources from writing to the device at once
        self.write_lock = threading.Lock()

        # dictionary that stores callback functions for various categories.
        # format is {"category1": [callback1, callback2, ...], "category2": [callbackA, callbackB, ...], ...}
        self.callbacks = {}

    def start(self):
        """Initializes the device"""
        raise NotImplementedError

    def flush(self):
        """Flushes all unread characters on the buffer"""
        raise NotImplementedError
    
    def available(self):
        raise NotImplementedError

    async def update(self):
        """
        Call this in a loop to read all buffered data and run callbacks.
        Throws HandshakeFailedException if a handshake expires
        :return: List[PacketResult] all packet results received this round (empty if no packets received)
        """
        self.check_handshakes()  # check pending handshakes. Throw an exception if any expired

        num_bytes = self.available()

        # if the device hasn't sent anything, don't do any parsing
        if num_bytes == 0:
            await asyncio.sleep(0.0)
            return []

        # read all characters available on the buffer
        recv_msg = self._read(num_bytes)
        self.buffer += recv_msg  # append characters to buffer. The buffer may have characters from last call

        # parse all characters on the buffer. Return any characters that weren't parsed into a packet
        # as well as any characters that might be worth notifying the user about
        remaining_buffer, self.buffer, results = self.protocol.parse_buffer(self.buffer)
        self.parse_debug_buffer(remaining_buffer)  # Print any debug messages received

        # no packets received. don't do any parsing
        if len(results) == 0:
            await asyncio.sleep(0.0)
            return []

        # call handle_result on each result received from TunnelProtocol.parse_buffer
        # put the result of that method into received
        received = []
        for result in results:
            result = await self.handle_result(result)
            if result is None:
                continue
            received.append(result)
        return received

    def register_callback(self, category: str, callback):
        """
        Pass a function reference. When a matching category is received, all registered callbacks will receive the data
        :param category: category to trigger callback
        :param callback: callable object
        :return: None
        """
        if category not in self.callbacks:
            self.callbacks[category] = []
        self.callbacks[category].append(callback)
        print("Registering callback for '%s' category. Num callbacks: %s" % (category, len(self.callbacks[category])))

    async def handle_result(self, result: PacketResult):
        """
        Internal method for handling a new packet result. Handles interaction with handshake confirm packets.
        Calls packet_callback as well as any registered callback functions

        :param result: PacketResult returned from TunnelProtocol
        :return: If it's not a handshake/confirm packet, the input result is returned unchanged.
                 If the PacketResult is an error, return None
        """

        # if the packet received has an error, don't check handshakes or send it to callbacks
        if self.protocol.is_code_error(result.error_code):
            return None

        # if the packet is confirming a previously engaged handshake, check pending handshakes
        if self.protocol.is_handshake_confirm(result):
            # extract info from the PacketResult and create a Handshake object to check against the pending objects
            handshake = Handshake.from_result(result)

            # if the device signaled that it didn't receive the packet correctly, print a warning and
            # don't process it
            if self.protocol.is_code_error(handshake.error_code):
                warnings.warn("Handshake confirm has an error code %s: %s" % (handshake, handshake.error_code))
                return None

            # check if parsed Handshake object matches any pending handshakes
            if handshake in self.pending_handshakes:
                self.pending_handshakes.remove(handshake)
                return handshake
            warnings.warn("Received confirm handshake, but no handshakes are expecting it! %s. Pending: %s" % (
                handshake, str(self.pending_handshakes)))

        future_result = self.find_matching_future_result(result)
        if future_result is not None:
            # if this result matches a future, copy the data so it can be used externally
            if self.debug:
                print("Found a matching get request: %s" % future_result)
            future_result.copy_from(result)
            future_result.set_event()

        # run callback method (doesn't anything do anything out of the box)
        await self.packet_callback(result)

        # run through registered callback functions if any match
        if result.category in self.callbacks:
            for callback in self.callbacks[result.category]:
                await callback(result)
        return result
    
    def find_matching_future_result(self, result: PacketResult):
        for future_result in self.pending_gets:
            if future_result.category == result.category:
                return future_result
        return None

    def check_handshakes(self):
        """Check if any handshakes expired or if any packets are due to be written again"""
        for handshake in self.pending_handshakes:
            if handshake.should_write_again():
                print("Writing handshake again %s" % handshake)
                self._write(handshake.packet)
            if handshake.did_fail():
                raise HandshakeFailedException("%s failed" % str(handshake))

    def parse_debug_buffer(self, remaining_buffer):
        """Print debug messages in the buffer delimited by the \n character"""
        index = 0
        # if the buffer contains protocol start characters, don't print it
        if remaining_buffer[0:1] == PACKET_START_0:
            return
        if remaining_buffer[1:2] == PACKET_START_1:
            return

        # call print for each delimiting character encountered
        while index < len(remaining_buffer):
            next_index = remaining_buffer.find(b'\n', index)
            if next_index == -1:
                break
            message = remaining_buffer[index: next_index]
            print("Device message:", message)
            index = next_index + 1

    async def get(self, category: str, formats: str, *args, timeout=None):
        """
        Write a packet and return the values of the next return packet with the same category.
        Raises asyncio.TimeoutError if timeout is reached.
        If timeout is None, this method will block indefinitely until a packet is found.
        """
        self.write_handshake(category, formats, *args)
        future_result = FuturePacketResult(category)
        self.pending_gets.append(future_result)
        await future_result.wait(timeout)
        self.pending_gets.remove(future_result)
        return future_result

    def write(self, category: str, formats: str, *args):
        """
        Write data to device as a TunnelProtocol packet

        :param category: str, category of packet. For packet routing on the receiving end. Must not contain:
            util.PACKET_SEP_STR (\t)
        :param formats: str, how to parse each of the provided arguments. Refer to TunnelProtocol.make_packet for keys.
        :param args: objects to interpret into a packet. Accepted types: int, str, bytes, float
        :return: None
        """
        self._write(self.protocol.make_packet(category, formats, *args))

    def write_handshake(self, category: str, formats: str, *args, write_interval=0.0, timeout=1.0):
        if write_interval > timeout:
            warnings.warn(
                "write_interval (%0.4f) is greater than timeout (%0.4f). Packet will not be rewritten" % (
                    write_interval, timeout)
            )
        handshake = self.protocol.make_handshake_packet(category, formats, *args, write_interval=write_interval, timeout=timeout)
        packet = handshake.packet
        self.pending_handshakes.append(handshake)
        self._write(packet)

    def _read(self, num_bytes):
        """Wrapper for device.read. Locks the device so multiple sources can't write at the same time"""
        raise NotImplementedError

    def _write(self, packet):
        """Wrapper for device.write. Locks the device so multiple sources can't write at the same time"""
        raise NotImplementedError

    async def packet_callback(self, result):
        """Override this method in a subclass. This gets called when any new correctly parsed packet arrives."""
        await asyncio.sleep(0.0)

    def stop(self):
        """Gracefully shutdown the device connection"""
        raise NotImplementedError
