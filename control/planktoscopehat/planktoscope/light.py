################################################################################
# Practical Libraries
################################################################################
# Logger library compatible with multiprocessing
from loguru import logger

import os, time

# Library for starting processes
import multiprocessing

# Basic planktoscope libraries
import planktoscope.mqtt

import RPi.GPIO

import subprocess  # nosec

# Library to send command over I2C for the light module on the fan
import smbus2 as smbus

import enum


logger.info("planktoscope.light is loaded")


def i2c_update():
    # Update the I2C Bus in order to really update the LEDs new values
    subprocess.Popen("i2cdetect -y 1".split(), stdout=subprocess.PIPE)  # nosec


class i2c_led:
    """
    LM36011 Led controller
    """

    @enum.unique
    class Register(enum.IntEnum):
        enable = 0x01
        configuration = 0x02
        flash = 0x03
        torch = 0x04
        flags = 0x05
        id_reset = 0x06

    DEVICE_ADDRESS = 0x64
    # This constant defines the current (mA) sent to the LED, 10 allows the use of the full ISO scale and results in a voltage of 2.77v
    DEFAULT_CURRENT = 10

    LED_selectPin = 18

    def __init__(self):
        self.VLED_short = False
        self.thermal_scale = False
        self.thermal_shutdown = False
        self.UVLO = False
        self.flash_timeout = False
        self.IVFM = False
        RPi.GPIO.setwarnings(False)
        RPi.GPIO.setmode(RPi.GPIO.BCM)
        RPi.GPIO.setup(self.LED_selectPin, RPi.GPIO.OUT)
        self.output_to_led1()
        self.on = False
        try:
            self.force_reset()
            if self.get_flags():
                logger.error("Flags raised in the LED Module, clearing now")
                self.VLED_short = False
                self.thermal_scale = False
                self.thermal_shutdown = False
                self.UVLO = False
                self.flash_timeout = False
                self.IVFM = False
            led_id = self.get_id()
        except (OSError, Exception) as e:
            logger.exception(f"Error with the LED control module, {e}")
            raise
        logger.debug(f"LED module id is {led_id}")

    def output_to_led1(self):
        logger.debug("Switching output to LED 1")
        RPi.GPIO.output(self.LED_selectPin, RPi.GPIO.HIGH)


    def get_id(self):
        led_id = self._read_byte(self.Register.id_reset)
        led_id = led_id & 0b111111
        return led_id

    def get_state(self):
        return self.on

    def force_reset(self):
        logger.debug("Resetting the LED chip")
        self._write_byte(self.Register.id_reset, 0b10000000)

    def get_flags(self): # this method checks the state of the LED and logs it out 
        flags = self._read_byte(self.Register.flags)
        self.flash_timeout = bool(flags & 0b1)
        self.UVLO = bool(flags & 0b10)
        self.thermal_shutdown = bool(flags & 0b100)
        self.thermal_scale = bool(flags & 0b1000)
        self.VLED_short = bool(flags & 0b100000)
        self.IVFM = bool(flags & 0b1000000)
        if self.VLED_short:
            logger.warning("Flag VLED_Short asserted")
        if self.thermal_scale:
            logger.warning("Flag thermal_scale asserted")
        if self.thermal_shutdown:
            logger.warning("Flag thermal_shutdown asserted")
        if self.UVLO:
            logger.warning("Flag UVLO asserted")
        if self.flash_timeout:
            logger.warning("Flag flash_timeout asserted")
        if self.IVFM:
            logger.warning("Flag IVFM asserted")
        return flags

    def set_flash_current(self, current):
        # From 11 to 1500mA
        # Curve is not linear for some reason, but this is close enough
        value = int(current * 0.085)
        logger.debug(f"Setting flash current to {value}")
        self._write_byte(self.Register.flash, value)

    def _write_byte(self, address, data):
        with smbus.SMBus(1) as bus:
            bus.write_byte_data(self.DEVICE_ADDRESS, address, data)

    def _read_byte(self, address):
        with smbus.SMBus(1) as bus:
            b = bus.read_byte_data(self.DEVICE_ADDRESS, address)
        return b


################################################################################
# Main Segmenter class
################################################################################
class LightProcess(multiprocessing.Process):
    """This class contains the main definitions for the light of the PlanktoScope"""

    def __init__(self, event):
        """Initialize the Light class

        Args:
            event (multiprocessing.Event): shutdown event
        """
        super(LightProcess, self).__init__(name="light")

        logger.info("planktoscope.light is initialising")

        self.stop_event = event
        self.light_client = None
        try:
            self.led = i2c_led()
            self.led.output_to_led1()
            time.sleep(0.5)
        except Exception as e:
            logger.error(
                f"We have encountered an error trying to start the LED module, stopping now, exception is {e}"
            )
            raise e
        else:
            logger.success("planktoscope.light is initialised and ready to go!")

    def led_off(self, led):
        logger.debug("Turning led 1 off")
        

    def led_on(self, led):
        logger.debug("Turning led 1 on")
        self.led.output_to_led1()

    @logger.catch
    def treat_message(self):
        last_message = None
        if self.light_client.new_message_received():
            logger.info("We received a new message")
            last_message = self.light_client.msg["payload"]
            logger.debug(last_message)
            self.light_client.read_message()
            if "action" not in last_message and "settings" not in last_message:
                logger.error(
                    f"The received message has the wrong argument {last_message}"
                )
                self.light_client.client.publish(
                    "status/light",
                    '{"status":"Received message did not contain action or settings"}',
                )
                return
        if last_message:
            if "action" in last_message:
                if last_message["action"] == "on":
                    # {"action":"on", "led":"1"}
                    logger.info("Turning the light on.")
                    self.led_on(0)
                    self.light_client.client.publish(
                        "status/light", '{"status":"Led 1: On"}'
                    )
                elif last_message["action"] == "off":
                    # {"action":"off", "led":"1"}
                    logger.info("Turn the light off.")
                    self.led_off(0)
                    self.light_client.client.publish(
                        "status/light", '{"status":"Led 1: Off"}'
                    )
                else:
                    logger.warning(
                        f"We did not understand the received request {last_message}"
                    )
            if "settings" in last_message:
                if "current" in last_message["settings"]:
                    # {"settings":{"current":"20"}}
                    current = last_message["settings"]["current"]
                    if self.led.get_state():
                        # Led is on, rejecting the change
                        self.light_client.client.publish(
                            "status/light",
                            '{"status":"Turn off the LED before changing the current"}',
                        )
                        return
                    logger.info(f"Switching the LED current to {current}mA")
                    try:
                        self.led.set_torch_current(current)
                    except:
                        self.light_client.client.publish(
                            "status/light",
                            '{"status":"Error while setting the current, power cycle your machine"}',
                        )
                    else:
                        self.light_client.client.publish(
                            "status/light", f'{{"status":"Current set to {current}mA"}}'
                        )
                else:
                    logger.warning(
                        f"We did not understand the received settings request in {last_message}"
                    )
                    self.light_client.client.publish(
                        "status/light",
                        f'{{"status":"Settings request not understood in {last_message}"}}',
                    )

    ################################################################################
    # While loop for capturing commands from Node-RED
    ################################################################################
    @logger.catch
    def run(self):
        """This is the function that needs to be started to create a thread"""
        logger.info(
            f"The light control thread has been started in process {os.getpid()}"
        )

        # MQTT Service connection
        self.light_client = planktoscope.mqtt.MQTT_Client(
            topic="light", name="light_client"
        )

        # Publish the status "Ready" to via MQTT to Node-RED
        self.light_client.client.publish("status/light", '{"status":"Ready"}')

        logger.success("Light module is READY!")

        # This is the loop
        while not self.stop_event.is_set():
            self.treat_message()
            time.sleep(0.1)

        logger.info("Shutting down the light process")
        self.led.set_flash_current(1)
        self.led.get_flags()
        RPi.GPIO.cleanup()
        self.light_client.client.publish("status/light", '{"status":"Dead"}')
        self.light_client.shutdown()
        logger.success("Light process shut down! See you!")
