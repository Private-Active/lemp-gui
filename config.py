#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2017, doudoudzj
# Copyright (c) 2012, VPSMate development team
# All rights reserved.
#
# InPanel is distributed under the terms of The New BSD License.
# The full license can be found in 'LICENSE'.

import datetime
from hashlib import md5
import hmac
import sys
import time
from base64 import b64decode
from os.path import dirname, join

from core.modules.configuration import configurations
from core.utils import is_valid_ip, randstr

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print('''Usage: %s option value

OPTIONS:
ip:              ip address (need restart)
port:            port number (need restart)
username:        username of admin account
password:        password of admin account
loginlock:       set the login lock. value: on or off
accesskey:       access key for remote access, must be empty
                 or a 64-bytes string with base64 encoded.
accesskeyenable: set the remote access switch. value: on or off
''' % sys.argv[0])
        sys.exit()

    data_path = join(dirname(__file__), 'data')
    config = configurations(data_path + '/config.ini')

    option, value = sys.argv[1:]
    if option == 'ip':
        if value != '*' and not is_valid_ip(value):
            print('Error: %s is not a valid IP address' % value)
            sys.exit(-1)
        config.set('server', 'ip', value)
    elif option == 'port':
        port = int(value)
        if not port > 0 and port < 65535:
            print('Error: port number should between 0 and 65535')
            sys.exit(-1)
        config.set('server', 'port', value)
    elif option == 'username':
        config.set('auth', 'username', value)
    elif option == 'password':
        key = randstr()
        hmd5 = md5(value).hexdigest()
        pwd = hmac.new(key, hmd5).hexdigest()
        config.set('auth', 'password', '%s:%s' % (pwd, key))
    elif option == 'loginlock':
        if value not in ('on', 'off'):
            print('Error: loginlock value should be either on or off')
            sys.exit(-1)
        if value == 'on':
            config.set('runtime', 'loginlock', 'on')
            config.set('runtime', 'loginfails', 0)
            config.set('runtime', 'loginlockexpire',
                       int(time.mktime(datetime.datetime.max.timetuple())))
        elif value == 'off':
            config.set('runtime', 'loginlock', 'off')
            config.set('runtime', 'loginfails', 0)
            config.set('runtime', 'loginlockexpire', 0)
    elif option == 'accesskey':
        if value != '':
            try:
                if len(b64decode(value)) != 32:
                    raise Exception()
            except:
                print('Error: invalid accesskey format')
                sys.exit(-1)
        config.set('auth', 'accesskey', value)
    elif option == 'accesskeyenable':
        if value not in ('on', 'off'):
            print('Error: accesskeyenable value should be either on or off')
            sys.exit(-1)
        config.set('auth', 'accesskeyenable', value)
