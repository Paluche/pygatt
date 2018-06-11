from __future__ import print_function

import threading
import logging
from collections import defaultdict
from binascii import hexlify
from uuid import UUID

from . import exceptions

try:
    string_type = basestring
except NameError:
    string_type = str

log = logging.getLogger(__name__)


class BLEDevice(object):
    """
    An BLE device connection instance, returned by one of the BLEBackend
    implementations. This class is not meant to be instantiated directly - use
    BLEBackend.connect() to create one.
    """
    def __init__(self, address):
        """
        Initialize.

        address -- the BLE address (aka MAC address) of the device as a string.
        """
        self._address = address
        self._services = {}
        self._callbacks = defaultdict(set)
        self._subscribed_handlers = {}
        self._lock = threading.Lock()

    def bond(self, permanent=False):
        """
        Create a new bond or use an existing bond with the device and make the
        current connection bonded and encrypted.
        """
        raise NotImplementedError()

    def get_rssi(self):
        """
        Get the receiver signal strength indicator (RSSI) value from the BLE
        device.

        Returns the RSSI value in dBm on success.
        Returns None on failure.
        """
        raise NotImplementedError()

    def char_read(self, uuid):
        """
        Reads a Characteristic by UUID.

        uuid -- UUID of Characteristic to read as a string.

        Returns a bytearray containing the characteristic value on success.

        Example:
            my_ble_device.char_read('a1e8f5b1-696b-4e4c-87c6-69dfe0b0093b')
        """
        raise NotImplementedError()

    def char_read_handle(self, uuid):
        """
        Reads a Characteristic by handle.

        handle -- handle of Characteristic to read.

        Returns a bytearray containing the characteristic value on success.

        Example:
            my_ble_device.char_read_handle(5)
        """
        raise NotImplementedError()

    def char_write(self, uuid, value, srvc_uuid=None, wait_for_response=False):
        """
        Writes a value to a given characteristic UUID.

        uuid -- the UUID of the characteristic to write to.
        value -- a bytearray to write to the characteristic.
        wait_for_response -- wait for response after writing.

        Example:
            my_ble_device.char_write('a1e8f5b1-696b-4e4c-87c6-69dfe0b0093b',
                                     bytearray([0x00, 0xFF]))
        """
        return self.char_write_handle(self.get_handle(uuid, srvc_uuid), value,
                                      wait_for_response=wait_for_response)

    def char_write_handle(self, handle, value, wait_for_response=False):
        """
        Writes a value to a given characteristic handle. This can be used to
        write to the characteristic config handle for a primary characteristic.

        hande -- the handle to write to.
        value -- a bytearray to write to the characteristic.
        wait_for_response -- wait for response after writing.

        Example:
            my_ble_device.char_write(42,
                                     bytearray([0x00, 0xFF]))
        """
        raise NotImplementedError()

    def disconnect(self):
        """
        Disconnect from the device. This instance of BLEDevice cannot be used
        after calling this method, you must call BLEBackend.connect() again to
        get a fresh connection.
        """
        raise NotImplementedError()

    def _notification_handles(self, uuid, srvc_uuid=None):
        # Expect notifications on the value handle...
        value_handle = self.get_handle(uuid, srvc_uuid)

        # but write to the characteristic config to enable notifications
        # TODO with the BGAPI backend we can be smarter and fetch the actual
        # characteristic config handle - we can also do that with gattool if we
        # use the 'desc' command, so we'll need to change the "get_handle" API
        # to be able to get the value or characteristic config handle.
        characteristic_config_handle = value_handle + 1

        return value_handle, characteristic_config_handle

    def subscribe(self, uuid, srvc_uuid=None, callback=None, indication=False):
        """
        Enable notifications or indications for a characteristic and register a
        callback function to be called whenever a new value arrives.

        uuid -- UUID as a string of the characteristic to subscribe.
        srvc_uuid -- UUID as a string of the service where the characteristic
                     to subscribe is.
        callback -- function to be called when a notification/indication is
                    received on this characteristic.
        indication -- use indications (where each notification is
                      acknowledged). This is more reliable, but slower.
        """

        value_handle, characteristic_config_handle = (
            self._notification_handles(uuid, srvc_uuid)
        )

        properties = bytearray([
            0x2 if indication else 0x1,
            0x0
        ])

        with self._lock:
            if callback is not None:
                self._callbacks[value_handle].add(callback)

            if self._subscribed_handlers.get(value_handle, None) != properties:
                self.char_write_handle(
                    characteristic_config_handle,
                    properties,
                    wait_for_response=False
                )
                log.info("Subscribed to uuid=%s", uuid)
                self._subscribed_handlers[value_handle] = properties
            else:
                log.debug("Already subscribed to uuid=%s", uuid)

    def unsubscribe(self, uuid):
        """
        Disable notification for a charecteristic and de-register the callback.
        """
        value_handle, characteristic_config_handle = (
            self._notification_handles(uuid)
        )

        properties = bytearray([0x0, 0x0])

        with self._lock:
            if value_handle in self._callbacks:
                del(self._callbacks[value_handle])
            if value_handle in self._subscribed_handlers:
                del(self._subscribed_handlers[value_handle])
                self.char_write_handle(
                    characteristic_config_handle,
                    properties,
                    wait_for_response=False
                )
                log.info("Unsubscribed from uuid=%s", uuid)
            else:
                log.debug("Already unsubscribed from uuid=%s", uuid)

    def __find_char(self, char_uuid, srvc_uuid=None):
        for srvc_uuid_str, srvc_obj in self._services.items():
            if srvc_uuid is None or srvc_uuid == srvc_obj:
                for char_uuid_str, char_obj in srvc_obj.characteristics.items():
                    if char_obj == char_uuid:
                        return char_obj
        return None

    def get_handle(self, char_uuid, srvc_uuid=None):
        """
        Look up and return the handle for an attribute by its UUID.
        :param char_uuid: The UUID of the characteristic.
        :type uuid: str or UUID
        :param srvc_uuid: The UUID of the service the characteristic is in.
        :type uuid: str or UUID
        :return: None if the UUID was not found.
        """
        if isinstance(char_uuid, string_type):
            char_uuid = UUID(char_uuid)

        if srvc_uuid is not None and isinstance(srvc_uuid, string_type):
            srvc_uuid = UUID(srvc_uuid)
        log.debug("Looking up handle for characteristic %s", char_uuid)

        characteristic = self.__find_char(char_uuid, srvc_uuid)

        if characteristic is None:
            # Reload the database to update it, with some luck the
            # characteristic will be in there now.
            self._services = self.discover()

        characteristic = self.__find_char(char_uuid, srvc_uuid)

        if characteristic is None:
            message = "No characteristic found matching %s %s" % \
                      (char_uuid, srvc_uuid if srvc_uuid is not None else '')
            log.warn(message)
            raise exceptions.BLEError(message)

        # TODO support filtering by descriptor UUID, or maybe return the whole
        # Characteristic object
        log.debug("Found %s" % characteristic)
        return characteristic.handle

    def receive_notification(self, handle, value):
        """
        Receive a notification from the connected device and propagate the value
        to all registered callbacks.
        """

        log.info('Received notification on handle=0x%x, value=0x%s',
                 handle, hexlify(value))
        with self._lock:
            if handle in self._callbacks:
                for callback in self._callbacks[handle]:
                    callback(handle, value)
