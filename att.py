from pynput import mouse, keyboard
from time import time
from urllib.request import Request, urlopen
from threading import Timer
from logzero import logger
import logzero
import logging
from os import getenv

SHOULD_TRACK_URL = getenv('ATT_TRACK_URL',
                          'http://localhost:1337/should-track')
IS_WORKING_URL = getenv('ATT_IS_WORKING_URL',
                        'http://localhost:1337/is-working')
STOP_WORKING_URL = getenv('ATT_STOP_WORKING_URL',
                          'http://localhost:1337/stop-working')

LOG_FILE = getenv('ATT_LOG_FILE')


if LOG_FILE != None:
    try:
        logzero.logfile(LOG_FILE)
    except:
        logger.error(f'Invalid log file {LOG_FILE}')


def rate_limit(rate):
    def decorator_limit(fn):
        global last_execution
        last_execution = 0§

        def wrapper(*args, **kwargs):
            global last_execution
            current_time = time()
            if current_time - last_execution > rate:
                last_execution = current_time
                fn(*args, **kwargs)

        return wrapper

    return decorator_limit


def send_post(url):
    request = Request(url, b'')
    try:
        urlopen(request)
    except:
        logger.warn(f'cannot reach url {url}')


def send_get(url):
    request = Request(url)

    content = 'n'

    try:
        content = urlopen(request).read()
    except:
        logger.warn(f'cannot reach url {url}')

    return content


class AliveNotifier:
    def __init__(self, url, interval):
        self.url = url
        self.interval = interval
        self.alive = False
        self.timer = None

    def notify(self):
        if self.alive:
            logger.info('alive notification')
            send_post(self.url)
            self.timer = Timer(self.interval, self.notify)
            self.timer.start()

    def stop(self):
        self.alive = False
        self.timer.cancel()

    def start(self):
        self.alive = True
        self.notify()

    def set_interval(self, interval):
        self.interval = interval


class Tracker:
    def __init__(self, max_idle_time, is_working_url, stop_working_url):
        self.is_working = None
        self.stopped_working_timer = None
        self.started = False
        self.max_idle_time = max_idle_time
        self.alive_notifier = AliveNotifier(is_working_url, max_idle_time/2)
        self.stop_working_url = stop_working_url

    def start(self):
        self.is_working = False
        self.started = True
        self.mouseListener = mouse.Listener(
            on_move=self.action_performed,
            on_click=self.action_performed,
            on_scroll=self.action_performed
        )

        self.keyboardListener = keyboard.Listener(
            on_press=self.action_performed)

        self.mouseListener.start()
        self.keyboardListener.start()

        self.mouseListener.join()
        self.keyboardListener.join()

    def stop(self):
        self.started = False
        self.mouseListener.stop()
        self.keyboardListener.stop()
        self.alive_notifier.stop()

        if self.is_working:
            self.stopped_working_timer.cancel()

    def is_running(self):
        return self.started

    def started_working(self):
        logger.info('start working')
        self.is_working = True
        self.alive_notifier.start()

    def stopped_working(self):
        self.is_working = False
        self.alive_notifier.stop()
        logger.info('stop working')
        send_post(self.stop_working_url)

    def set_max_idle_time(self, value):
        if self.max_idle_time != value:
            logger.info(
                f'update max idle time from {self.max_idle_time} to {value}')
            self.max_idle_time = value
            self.alive_notifier.set_interval(value)

    @rate_limit(1)
    def action_performed(self, *args):
        if self.is_working:
            self.stopped_working_timer.cancel()
        else:
            self.started_working()

        self.is_working = True
        self.stopped_working_timer = Timer(
            self.max_idle_time, self.stopped_working)
        self.stopped_working_timer.start()


class TrackerManager:
    def __init__(self, should_track_url, is_working_url, stop_working_url):
        self.should_track_url = should_track_url
        self.is_working_url = is_working_url
        self.stop_working_url = stop_working_url
        self.tracker = Tracker(0, is_working_url, stop_working_url)
        self.timer = None

    def start(self):
        self.check_should_track()

    def check_should_track(self):
        response = send_get(self.should_track_url).decode('utf-8')

        self.timer = Timer(10, self.check_should_track)
        self.timer.start()

        if response == 'n':
            logger.info('should not track')
            if self.tracker.is_running():
                self.tracker.stop()
        else:
            logger.info(f'should track with max_idle_time={int(response)}')
            self.tracker.set_max_idle_time(int(response))
            if not self.tracker.is_running():
                self.tracker.start()

    def stop(self):
        if self.timer:
            self.timer.cancel()

        if self.tracker.is_running():
            self.tracker.stop()


tracker_manager = TrackerManager(
    SHOULD_TRACK_URL, IS_WORKING_URL, STOP_WORKING_URL)

tracker_manager.start()
