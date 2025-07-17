# -*- coding: utf-8 -*-
#
# Copyright (c) 2017 - 2019, doudoudzj
# All rights reserved.
#
# InPanel is distributed under the terms of the New BSD License.
# The full license can be found in 'LICENSE'.

'''Module for vsftpd Management'''

from core.utils import cfg_get_array, cfg_set_array

config_file = '/etc/vsftpd/vsftpd.conf'
delimiter = '='

base_configs = {
    'anonymous_enable':        '',
    'local_enable':            '',
    'local_umask':             '',
    'anon_upload_enable':      '',
    'anon_mkdir_write_enable': '',
    'dirmessage_enable':       '',
    'xferlog_enable':          '',
    'connect_from_port_20':    '',
    'chown_upload':            '',
    'chown_username':          '',
    'xferlog_file':            '',
    'xferlog_std_format':      '',
    'idle_session_timeout':    '',
    'data_connection_timeout': '',
    'nopriv_user':             '',
    'async_abor_enable':       '',
    'ascii_upload_enable':     '',
    'ascii_download_enable':   '',
    'ftpd_banner':             '',
    'deny_email_enable':       '',
    'banned_email_file':       '',
    'chroot_list_enable':      '',
    'chroot_list_file':        '',
    'max_clients':             '',
    'message_file':            '',
}


def get_config():
    array_configs = cfg_get_array(config_file, base_configs, delimiter)
    return array_configs


def set_config():
    result = cfg_set_array(config_file, base_configs, delimiter)
    return result
