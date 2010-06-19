#!/usr/bin/env python
# -*- encoding: utf-8 -*-
import datetime
import imp
import itertools
import os
import sys
import time
import traceback
from collections import defaultdict

import yaml

from BufferingBot import BufferingBot, Message

import feeds
from util import trace, format_time, to_datetime, LocalTimezone

DONT_SEND_ANYTHING = False

class FeedBot(BufferingBot):
    def __init__(self, config_file_name):
        self.config = None
        self.config_file_name = config_file_name
        self.buffer_file_name = os.path.join(FEEDEX_ROOT, 'buffer.yml')
        self.version = -1
        self.config_timestamp = -1
        self.load()

        server = self.config['server']
        nickname = self.config['nickname']
        BufferingBot.__init__(self, [server], nickname,
            realname=b'FeedEx the feed bot',
            buffer_timeout=-1, # don't use timeout
            use_ssl=self.config.get('use_ssl', False))

        trace("Loading buffer...")
        if os.access(self.buffer_file_name, os.F_OK):
            for message in yaml.load(open(self.buffer_file_name, 'rb')):
                self.push_message(message)

        self.initialized = False
        self.connection.add_global_handler('welcome', self._on_connected)

        self.autojoin_channels = set()
        self.feeds = defaultdict(list)
        self.feed_iter = itertools.cycle(self.feeds)
        self.handlers = []
        self.frequent_fetches = {}

        self.reload_feed()

        self._check_config_file()

    def _get_config_time(self):
        if not os.access(self.config_file_name, os.F_OK):
            return -1
        return os.stat(self.config_file_name).st_mtime

    def _get_config_data(self):
        if not os.access(self.config_file_name, os.R_OK):
            return None
        try:
            return eval(open(self.config_file_name).read())
        except SyntaxError:
            traceback.print_exc()
        return None

    def _check_config_file(self):
        try:
            if self._get_config_time() <= self.config_timestamp:
                return
            self.reload()
        except Exception:
            traceback.print_exc()
        self.ircobj.execute_delayed(1, self._check_config_file)

    def _on_connected(self, conn, _):
        trace('Connected.')
        try:
            for channel in self.autojoin_channels:
                self.connection.join(channel.encode('utf-8'))
        except: #TODO: specify exception here
            pass
        if self.initialized:
            return
        if conn != self.connection:
            return
        self.ircobj.execute_delayed(0, self._iter_feed)
        self.initialized = True

    def frequent_fetch(self, fetcher):
        if fetcher not in self.frequent_fetches:
            raise StopIteration()
        if not self.frequent_fetches[fetcher]:
            raise StopIteration()
        self.fetch_feed(fetcher)
        self.ircobj.execute_delayed(
            self.config.get('frequent_fetch_period', 20), self.frequent_fetch)

    def _iter_feed(self):
        if not self.feeds:
            return
        if self.feed_iter is None:
            self.feed_iter = itertools.cycle(self.feeds)
        try:
            fetcher = next(self.feed_iter)
        except StopIteration:
            self.feed_iter = itertools.cycle(self.feeds)
            fetcher = next(self.feed_iter)
        except RuntimeError:
            # RuntimeError: dictionary changed size during iteration
            self.feed_iter = itertools.cycle(self.feeds)
            fetcher = next(self.feed_iter)
        self.fetch_feed(fetcher)
        self.ircobj.execute_delayed(
            self.config.get('fetch_period', 3), self._iter_feed)

    def fetch_feed(self, fetcher):
        entries = []
        try:
            entries = fetcher.get_fresh_entries()
        except Exception:
            trace('An error occured while trying to get %s:' % fetcher.uri)
            traceback.print_exc(limit=None)
            return
        for formatter in self.feeds[fetcher]:
            try:
                for target, msg, opt in formatter.format_entries(entries):
                    assert isinstance(target, str)
                    assert isinstance(msg, str)
                    dt = datetime.datetime.fromtimestamp(opt.get('timestamp', 0))
                    message = Message('privmsg', (target, msg), timestamp=dt)
                    self.push_message(message)
            except Exception:
                print('An error occured while trying to format an entries from %s:' % fetcher.uri)
                traceback.print_exc(limit=None)
                return
        if entries:
            try:
                fetcher.update_timestamp(entries)
            except Exception:
                print('An error occured while updating timestamp for %s:' % fetcher.uri)
                traceback.print_exc(limit=None)
                return

    def flood_control(self):
        if BufferingBot.flood_control(self):
            self.dump_buffer()

    def pop_buffer(self, message_buffer):
        earliest = message_buffer.peek().timestamp
        if earliest > time.time():
            # 미래에 보여줄 것은 미래까지 기다림
            # TODO: ignore_time이면 이 조건 무시
            return False
        BufferingBot.pop_buffer(self, message_buffer)
        return True

    def dump_buffer(self):
        dump = yaml.dump(list(self.buffer.dump()),
            default_flow_style=False,
            encoding='utf-8',
            allow_unicode=True)
        open(self.buffer_file_name, 'wb').write(dump)

    def push_message(self, message):
        BufferingBot.push_message(self, message)
        self.dump_buffer()

    def load(self):
        data = self._get_config_data()
        if self.version >= data['version']:
            return False
        self.config = data
        self.config_timestamp = os.stat(self.config_file_name).st_mtime
        self.version = data['version']
        return True

    def reload(self):
        if not self.load():
            return False
        trace("reloading...")
        self.reload_feed()
        trace("reloaded.")
        return True

    def reload_feed(self):
        self.handlers = []
        self._reload_feed_handlers()
        self._reload_feed_data()
        if self.initialized:
            for channel in self.autojoin_channels:
                channel = channel.encode('utf-8')
                if channel not in self.channels:
                    self.connection.join(channel)
            for fetcher, enabled in self.frequent_fetches.items():
                self.ircobj.execute_delayed(0, self.frequent_fetch, (fetcher,))
                self.frequent_fetches[fetcher] = enabled

    def _reload_feed_handlers(self):
        self.autojoin_channels = set()
        self.handlers = feeds.reload()

    def _load_feed_data(self):
        self.feed_iter = None
        self.feeds = defaultdict(list)
        self.autojoin_channels = set()
        self.frequent_fetches = {}
        for handler in self.handlers:
            manager = handler['manager']
            try:
                for fetcher, formatter in manager.load():
                    self.feeds[fetcher].append(formatter)
                    self.autojoin_channels.add(formatter.target)
                    if fetcher.frequent:
                        self.frequent_fetches[fetcher] = True
            except Exception:
                traceback.print_exc()
                continue
            trace('%s loaded successfully.' % handler['__name__'])
        if DONT_SEND_ANYTHING:
            self.autojoin_channels = set()
        for channel in self.channels:
            if channel.decode('utf-8', 'ignore') not in self.autojoin_channels:
                self.connection.part(channel)

    def _reload_feed_data(self):
        self._load_feed_data()

FEEDEX_ROOT = os.path.dirname(os.path.abspath(__file__))

def main():
    profile = None
    if len(sys.argv) > 1:
        profile = sys.argv[1]
    if not profile:
        profile = 'config'
    trace("profile: %s" % profile)
    config_file_name = os.path.join(FEEDEX_ROOT, '%s.py' % profile)
    feedex = FeedBot(config_file_name)
    feedex.start()

if __name__ == '__main__':
    main()

