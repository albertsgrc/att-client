#!/usr/bin/env /usr/local/bin/python3

import time
import json
from threading import Timer
from os import getenv, getcwd
import os
import re
import sys
import fcntl
from signal import signal, SIGTERM
import atexit

from git import Repo
from pynput import mouse, keyboard
import click
from logzero import logger
import logzero
import inquirer
import validators
import requests


from configstore import ConfigStore


def get_current_branch_name(repo_path):
    return Repo(repo_path).active_branch.name


def get_remote_info(repo_path):
    url = Repo(repo_path).remotes.origin.url
    match = re.match(r"^(git@|https:\/\/)([\w.]+)(?:\/|:)(.*\/*.)\.git$", url)

    return {
        "hostname": match.group(2),
        "project": match.group(3)
    }


def client_data():
    data_dict = conf.all()

    repository_path = conf.get('repositoryPath')

    remote_info = get_remote_info(repository_path)

    return {
        "gitlabHostname": remote_info['hostname'],
        "gitlabProject": remote_info['project'],
        "gitlabToken": data_dict['gitlabToken'],
        "togglToken": data_dict['togglToken'],
        "gitBranch": get_current_branch_name(repository_path)
    }


def rate_limit(rate):
    def decorator_limit(fn):
        global last_execution
        last_execution = 0

        def wrapper(*args, **kwargs):
            global last_execution
            current_time = time.time()
            if current_time - last_execution > rate:
                last_execution = current_time
                fn(*args, **kwargs)

        return wrapper

    return decorator_limit


def send_post(url, json={}):
    try:
        requests.post(url, json=json)
    except:
        logger.error(f'Cannot POST {url}')


def send_get(url):
    result = {}

    try:
        result = requests.get(url).json()
    except:
        logger.error(f'Cannot GET {url}')

    return result


class AliveNotifier:
    def __init__(self, url, interval):
        self.url = url
        self.interval = interval
        self.alive = False
        self.timer = None

    def notify(self):
        if self.alive:
            send_post(self.url, client_data())
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
    def __init__(self, max_idle_time, set_is_working_url, set_not_working_url):
        self.is_working = None
        self.stopped_working_timer = None
        self.started = False
        self.max_idle_time = max_idle_time
        self.alive_notifier = AliveNotifier(
            set_is_working_url, max_idle_time//2)
        self.set_not_working_url = set_not_working_url

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

        if self.is_working:
            self.stopped_working()
            self.stopped_working_timer.cancel()

    def is_running(self):
        return self.started

    def started_working(self):
        logger.info('Start working')
        self.is_working = True
        self.alive_notifier.start()

    def stopped_working(self):
        self.is_working = False
        self.alive_notifier.stop()
        logger.info('Stop working')
        send_post(self.set_not_working_url, client_data())

    def set_max_idle_time(self, value):
        if self.max_idle_time != value:
            logger.info(
                f'Update max idle time from {self.max_idle_time} to {value}')
            self.max_idle_time = value
            self.alive_notifier.set_interval(value//2)

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
    def __init__(self, should_track_url, set_is_working_url, set_not_working_url):
        self.should_track_url = should_track_url
        self.set_is_working_url = set_is_working_url
        self.set_not_working_url = set_not_working_url
        self.tracker = Tracker(0, set_is_working_url, set_not_working_url)
        self.timer = None

    def start(self):
        self.check_should_track()

    def check_should_track(self):
        response = send_get(self.should_track_url)

        self.timer = Timer(10, self.check_should_track)
        self.timer.start()

        if 'maxIdleTime' in response:
            max_idle_time = response['maxIdleTime']
            logger.info(f'Should track with max_idle_time={max_idle_time}')
            self.tracker.set_max_idle_time(max_idle_time)
            if not self.tracker.is_running():
                self.tracker.start()
        else:
            logger.info('Should not track')
            if self.tracker.is_running():
                self.tracker.stop()

    def stop(self):
        if self.timer:
            self.timer.cancel()

        if self.tracker.is_running():
            self.tracker.stop()


def is_git_repo(path):
    is_repo = True
    try:
        Repo(path)
    except:
        is_repo = False

    return is_repo


def initialize_config(conf):
    def gitlabTokenValidator(others, token):
        if token == '':
            return True

        repository_path = others['repositoryPath']

        info = get_remote_info(repository_path)
        hostname = info['hostname']
        result = requests.get(f'https://{hostname}/api/v4/version',
                              headers={"private-token": token})

        if result.status_code == 404:
            print('')
            logger.error(
                f'Repository {repository_path} with remote {hostname} is probably not a gitlab remote')

        return result.status_code == 200

    def togglTokenValidator(_, token):
        result = requests.get('https://www.toggl.com/api/v8/me',
                              auth=(token, 'api_token'))

        return result.status_code == 200

    def gitRepoValidator(_, path):
        return is_git_repo(path)

    def serverUrlValidator(_, url):
        return validators.url(url)

    def transformServerUrl(url):
        if not url.endswith('/'):
            return url + '/'
        else:
            return url

    repoDefault = (getcwd() + '/') if is_git_repo(getcwd()) else None

    questions = [
        inquirer.Text('repositoryPath', message="Enter your repository path",
                      default=repoDefault, validate=gitRepoValidator),
        inquirer.Text('gitlabToken', message="Enter your gitlab token", default='',
                      validate=gitlabTokenValidator),
        inquirer.Text('togglToken', message="Enter your toggl token",
                      validate=togglTokenValidator),
        inquirer.Text(
            'serverUrl', message="Enter the tracking server url", validate=serverUrlValidator),
        inquirer.Path('logsFile', message="Enter the logs file path",
                      path_type=inquirer.Path.FILE, default="/tmp/asrtt.log")
    ]

    answers = inquirer.prompt(questions)

    answers['serverUrl'] = transformServerUrl(answers['serverUrl'])

    conf.all(answers)


@click.group()
def cli():
    pass


def start_tracking():
    global tracker_manager

    if os.path.isfile(pidPath):
        with open(pidPath, "r") as old_pidfile:
            global old_pid
            old_pid = old_pidfile.read()

    try:
        lockfile = open(pidPath, "w")
    except IOError:
        print(f'Asrtt is already running')
        sys.exit(1)

    try:
        # Try to get an exclusive lock on the file. This will fail if another process has the file
        # locked.
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print(f'Asrtt is already running')
        with open(pidPath, "w") as pidfile:
            pidfile.write(old_pid)
        sys.exit(1)

    def sigterm(_, i):
        tracker_manager.stop()
        try:
            sys.exit(0)
        except:
            pass

    def mexit():
        tracker_manager.stop()
        os.remove(pidPath)
        sys.exit(0)

    signal(SIGTERM, sigterm)
    atexit.register(mexit)

    try:
        lockfile.write("%s" % (os.getpid()))
        lockfile.flush()
    except IOError:
        print("Unable to write pid to the pidfile.")
        sys.exit(1)

    server_url = conf.get('serverUrl')

    should_track_url = server_url + 'should-track'
    set_is_working_url = server_url + 'set-is-working'
    set_not_working_url = server_url + 'set-not-working'

    tracker_manager = TrackerManager(
        should_track_url, set_is_working_url, set_not_working_url)

    tracker_manager.start()


@click.command('start', short_help='Start tracking time')
def start():
    start_tracking()


@click.command('stop', short_help='Stop tracking time')
def stop():
    if os.path.isfile(pidPath):
        with open(pidPath, "r") as old_pidfile:
            old_pid = old_pidfile.read()
            print('Stopping asrtt')
            os.kill(int(old_pid), SIGTERM)
    else:
        print('Asrtt is not tracking')


@click.command('restart', short_help='Restart att')
def restart():
    if os.path.isfile(pidPath):
        with open(pidPath, "r") as old_pidfile:
            old_pid = old_pidfile.read()
            print('stopping asrtt')
            os.kill(int(old_pid), SIGTERM)
    else:
        print('Asrtt is not tracking')

    start_tracking()


@click.command('reset-config', short_help='Reset configuration')
def reset_config():
    if not justInitialized:
        initialize_config(conf)


@click.command('get-repo', short_help='Print the current git repository directory')
def get_repo():
    current = conf.get('repositoryPath')
    logger.info(
        f'Current repository path is {current}')


@click.command('set-repo', short_help='Set the current git repository directory')
@click.option('--path', '-p', default=getcwd(), help="Set the repository directory")
def set_repo(path):
    if not is_git_repo(path):
        logger.error(f'Invalid repository path {path}')
    else:
        conf.set('repositoryPath', path)
        logger.info(
            f'Repository path set to {path}. \nYou don\'t need to restart asrtt to apply the changes.')


@click.command('get-config', short_help='Print the current configuration')
def get_config():
    print(json.dumps(conf.all(), sort_keys=True, indent=4))


cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(reset_config)
cli.add_command(set_repo)
cli.add_command(get_repo)
cli.add_command(get_config)


def main():
    global pidPath
    pidPath = "/tmp/asrtt.pid"

    global conf
    conf = ConfigStore('asrtt')

    global justInitialized
    justInitialized = conf.size == 0

    if conf.size == 0:
        initialize_config(conf)

    logzero.logfile(conf.get('logsFile'))

    cli()


if __name__ == "__main__":
    main()

# Git: https://www.pygit2.org/references.html#the-head
# head = repo.lookup_reference('HEAD').resolve()
# head = repo.head
# branch_name = head.name

# Gitlab: https://python-gitlab.readthedocs.io/en/stable/gl_objects/issues.html
