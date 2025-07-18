# -*- coding: utf-8 -*-
#
# Copyright (c) 2017, doudoudzj
# Copyright (c) 2012, VPSMate development team
# All rights reserved.
#
# InPanel is distributed under the terms of The New BSD License.
# The full license can be found in 'LICENSE'.

'''Module for Web Querying'''

import binascii
import hmac
import re
import time
from base64 import b64decode, b64encode
from datetime import datetime
from functools import partial
from json import dumps, loads
from logging import info as loginfo
from os import mkdir, stat, unlink
from os.path import abspath, basename, dirname, exists, isdir, isfile
from os.path import join as joinpath
from subprocess import PIPE, STDOUT, Popen
from uuid import uuid4

import core
# import httplib
import pyDes
import tornado
import tornado.gen
import tornado.httpclient
import tornado.ioloop
import tornado.web
from async_process import call_subprocess, callbackable
from core import api as core_api
from core import utils, releasetime
from modules import (aliyuncs, apache, cron, fdisk, files, ftp,
                     lighttpd, mysql, named, nginx, php, proftpd,
                     pureftpd, remote, shell, ssh, user, vsftpd, yum)
from modules.configuration import configurations
from modules.sc import ServerSet
from modules.server import ServerInfo, ServerTool
from modules.service import Service
from tornado.escape import to_unicode as _d
from tornado.escape import utf8 as _u

try:
    from shlex import quote  # For Python 3
    from urllib.request import urlopen, Request  # Python 3
except ImportError:
    from urllib2 import urlopen, Request  # Python 2
    from pipes import quote  # For Python 2


class Application(tornado.web.Application):
    def __init__(self, handlers=None, default_host="", transforms=None,
                 wsgi=False, **settings):
        dist = ServerInfo.dist()
        settings['dist_name'] = dist['name'].lower()
        settings['dist_version'] = dist['version']
        settings['dist_verint'] = int(dist['version'][0:dist['version'].find('.')])
        uname = ServerInfo.uname()
        settings['arch'] = uname['machine']
        if settings['arch'] == 'i686' and settings['dist_verint'] == 5: settings['arch'] = 'i386'
        #if settings['arch'] == 'unknown': settings['arch'] = uname['machine']
        settings['data_path'] = abspath(settings['data_path'])
        settings['package_path'] = joinpath(settings['data_path'], 'packages')

        tornado.web.Application.__init__(self, handlers, default_host, transforms,
                 wsgi, **settings)


class RequestHandler(tornado.web.RequestHandler):

    def initialize(self):
        """Parse JSON data to argument list.
        """
        self.inifile = joinpath(self.settings['conf_path'])
        self.config = configurations(self.inifile)

        content_type = self.request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            try:
                arguments = loads(tornado.escape.native_str(self.request.body))
                for name, value in arguments.items():
                    name = _u(name)
                    if isinstance(value, unicode):
                        value = _u(value)
                    elif isinstance(value, bool):
                        value = value and 'on' or 'off'
                    else:
                        value = ''
                    self.request.arguments.setdefault(name, []).append(value)
            except:
                pass

    def set_default_headers(self):
        self.set_header('Server', core.name)
        if 'Origin' in self.request.headers:
            self.set_header('Access-Control-Allow-Origin', self.request.headers.get('Origin'))
            self.set_header('Access-Control-Allow-Headers', 'X-ACCESS-TOKEN, Content-Type')
            self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

    def check_xsrf_cookie(self):
        # check for the access token
        if self.get_argument("_access", None) or self.request.headers.get("X-ACCESS-TOKEN"):
            if self.config.get('auth', 'accesskeyenable') != 'on':
                raise tornado.web.HTTPError(403, "Access Token Not Allowed")
        else:
            # check xsrf cookie
            token = (self.get_argument("_xsrf", None) or self.request.headers.get("X-XSRF-TOKEN"))
            if not token:
                raise tornado.web.HTTPError(403, "'_xsrf' argument missing from POST")
            if self.xsrf_token != token:
                raise tornado.web.HTTPError(403, "XSRF cookie does not match POST argument")

    def authed(self):
        # check for the access token
        access_token = (self.get_argument("_access", None) or self.request.headers.get("X-ACCESS-TOKEN"))
        if access_token:
            if self.config.get('auth', 'accesskeyenable') != 'on':
                raise tornado.web.HTTPError(403, 'Access Token Not Allowed')
            elif access_token != self.config.get('auth', 'accesskey'):
                raise tornado.web.HTTPError(403, 'Access Token Error')
        else:
            # get the cookie within 30 mins
            if self.get_secure_cookie('authed', None, 30.0/1440) == 'yes':
                # regenerate the cookie timestamp per 5 mins
                if self.get_secure_cookie('authed', None, 5.0/1440) == None:
                    self.set_secure_cookie('authed', 'yes', None)
            else:
                raise tornado.web.HTTPError(403, "Please Login First")

    def getlastactive(self):
        # get last active from cookie
        cv = self.get_cookie('authed', False)
        try:
            return int(cv.split('|')[1])
        except:
            return 0

    @property
    def xsrf_token(self):
        if not hasattr(self, "_xsrf_token"):
            token = self.get_cookie("XSRF-TOKEN") #  or self.request.headers.get("X-XSRF-TOKEN"))
            # token = (self.get_cookie("XSRF-TOKEN") or self.request.headers.get("X-XSRF-TOKEN"))
            if not token:
                token = binascii.b2a_hex(uuid4().bytes)
                expires_days = 30 if self.current_user else None
                self.set_cookie("XSRF-TOKEN", token, expires_days=expires_days)
            self._xsrf_token = token
        return self._xsrf_token


class IndexHandler(tornado.web.StaticFileHandler):
    def set_default_headers(self):
        self.set_header('Server', core.name)

    def get(self):
        with open(self.settings['index_path']) as f:
            html = f.read()
            # html = html.replace('<link rel="stylesheet" href="', '<link rel="stylesheet" href="/inpanel/')
            # html = html.replace('<script src="', '<script src="/inpanel/')
            html = html.replace("{{ template_path }}", "")
            html = html.replace("{{ releasetime }}", releasetime)
            self.write(html)


class StaticFileHandler(tornado.web.StaticFileHandler):
    def set_default_headers(self):
        self.set_header('Server', core.name)


class ErrorHandler(tornado.web.ErrorHandler):
    def set_default_headers(self):
        self.set_header('Server', core.name)


class FallbackHandler(tornado.web.FallbackHandler):
    def set_default_headers(self):
        self.set_header('Server', core.name)


class RedirectHandler(tornado.web.RedirectHandler):
    def set_default_headers(self):
        self.set_header('Server', core.name)


class FileDownloadHandler(StaticFileHandler):
    def get(self, path):
        self.authed()
        self.set_header('Content-Type', 'application/octet-stream')
        self.set_header('Content-disposition', 'attachment; filename=%s' % basename(path))
        self.set_header('Content-Transfer-Encoding', 'binary')
        StaticFileHandler.get(self, path)

    def authed(self):
        # check for the access token
        access_token = (self.get_argument("_access", None) or self.request.headers.get("X-ACCESS-TOKEN"))
        if access_token and self.config.get('auth', 'accesskeyenable') == 'on':
            if access_token != self.config.get('auth', 'accesskey'):
                raise tornado.web.HTTPError(403, 'Access Token Error')
                # print('access_token matched')
                # return
        else:
            # get the cookie within 30 mins
            if self.get_secure_cookie('authed', None, 30.0/1440) == 'yes':
                # regenerate the cookie timestamp per 5 mins
                if self.get_secure_cookie('authed', None, 5.0/1440) == None:
                    self.set_secure_cookie('authed', 'yes', None)
            else:
                raise tornado.web.HTTPError(403, "Please login first")


class FileUploadHandler(RequestHandler):
    def post(self):
        self.authed()
        path = self.get_argument('path', '/')

        self.write(u'<body style="font-size:14px;overflow:hidden;margin:0;padding:0;">')

        if not 'ufile' in self.request.files:
            self.write(u'请选择要上传的文件！')
        else:
            self.write(u'正在上传...<br>')
            for item in self.request.files['ufile']:
                filename = re.split('[\\\/]', item['filename'])[-1]
                with open(joinpath(path, filename), 'wb') as f:
                    f.write(item['body'])
                self.write(u'%s 上传成功！<br>' % item['filename'])

        self.write('</body>')


class FilePreviewHandler(RequestHandler):
    '''FilePreviewHandler
    TODO: support multi
    '''
    def get(self, path):
        self.authed()
        p = joinpath('/', path)
        if not exists(p):
            # logger.error("Kerberos failure: %s", err)
            raise tornado.web.HTTPError(404, 'File Not Found', reason='File Not Found')
        buffer = ''
        mtype = 'image/png'
        with open(p, 'rb') as f:
            buffer = f.read()
        data = {
            'mtype': 'image/png',
            'data': 'data:%s;base64,%s' % (mtype, str(b64encode(buffer), "utf-8"))
        }
        self.render('file/preview.html', **data)


class VersionHandler(RequestHandler):
    def get(self):
        self.authed()
        self.write({'code': 0, 'data': core.version_info})


class XsrfHandler(RequestHandler):
    """Write a XSRF token to cookie
    """
    def get(self):
        self.xsrf_token


class AuthStatusHandler(RequestHandler):
    """Check if client has been authorized
    """
    def get(self):
        self.write({'lastactive': self.getlastactive()})

    def post(self):
        # authorize and update cookie
        try:
            self.authed()
            self.write({'authed': 'yes'})
        except:
            self.write({'authed': 'no'})


class ClientHandler(RequestHandler):
    """Get client infomation.
    """
    def get(self, argument):
        if argument == 'ip':
            self.write(self.request.remote_ip)


class LoginHandler(RequestHandler):
    """Validate username and password.
    """
    def post(self):
        username = self.get_argument('username', '')
        password = self.get_argument('password', '')

        loginlock = self.config.get('runtime', 'loginlock')
        if self.config.get('runtime', 'mode') == 'demo': loginlock = 'off'

        # check if login is locked
        if loginlock == 'on':
            loginlockexpire = self.config.getint('runtime', 'loginlockexpire')
            if time.time() < loginlockexpire:
                self.write({'code': -1,
                    'msg': u'登录已被锁定，请在 %s 后重试登录。<br>'\
                        u'如需立即解除锁定，请在服务器上执行以下命令：<br>'\
                        u'/usr/local/inpanel/config.py loginlock off' %
                        datetime.fromtimestamp(loginlockexpire)
                            .strftime('%Y-%m-%d %H:%M:%S')})
                return
            else:
                self.config.set('runtime', 'loginlock', 'off')
                self.config.set('runtime', 'loginlockexpire', 0)

        loginfails = self.config.getint('runtime', 'loginfails')
        cfg_username = self.config.get('auth', 'username')
        cfg_password = self.config.get('auth', 'password')
        if cfg_password == '':
            self.write({'code': -1,
                'msg': u'登录密码还未设置，请在服务器上执行以下命令进行设置：<br>'\
                    u'/usr/local/inpanel/config.py password \'您的密码\''})
        elif username != cfg_username:  # wrong with username
            self.write({'code': -1, 'msg': u'用户不存在！'})
        else:   # username is corret
            cfg_password, key = cfg_password.split(':')
            if hmac.new(key, password).hexdigest() == cfg_password:
                if loginfails > 0:
                    self.config.set('runtime', 'loginfails', 0)
                self.set_secure_cookie('authed', 'yes', None)

                passwordcheck = self.config.getboolean('auth', 'passwordcheck')
                if passwordcheck:
                    self.write({'code': 1, 'msg': u'%s，您已登录成功！' % username})
                else:
                    self.write({'code': 0, 'msg': u'%s，您已登录成功！' % username})
            else:
                if self.config.get('runtime', 'mode') == 'demo':
                    self.write({'code': -1, 'msg': u'用户名或密码错误！'})
                    return
                loginfails = loginfails+1
                self.config.set('runtime', 'loginfails', loginfails)
                if loginfails >= 5:
                    # lock 24 hours
                    self.config.set('runtime', 'loginlock', 'on')
                    self.config.set('runtime', 'loginlockexpire', int(time.time())+86400)
                    self.write({'code': -1, 'msg': u'用户名或密码错误！<br>'\
                        u'已连续错误 5 次，登录已被禁止！'})
                else:
                    self.write({'code': -1, 'msg': u'用户名或密码错误！<br>'\
                        u'连续错误 5 次后将被禁止登录，还有 %d 次机会。' % (5-loginfails)})


class LogoutHandler(RequestHandler):
    """Logout
    """
    def post(self):
        self.authed()
        self.clear_cookie('authed')


class SitePackageHandler(RequestHandler):
    """Interface for querying site packages information.
    """

    def get(self, op):
        self.authed()
        if hasattr(self, op):
            getattr(self, op)()
        else:
            self.write({'code': -1, 'msg': u'未定义的操作！'})


    @tornado.web.asynchronous
    @tornado.gen.engine
    def getlist(self):
        if not exists(self.settings['package_path']): mkdir(self.settings['package_path'])

        packages = ''
        packages_cachefile = joinpath(self.settings['package_path'], '.meta')

        # fetch from cache
        if exists(packages_cachefile):
            # check the file modify time
            mtime = stat(packages_cachefile).st_mtime
            if time.time() - mtime < 86400: # cache 24 hours
                with open(packages_cachefile) as f: packages = f.read()

        # fetch from api
        if not packages:
            http = tornado.httpclient.AsyncHTTPClient()
            response = yield tornado.gen.Task(http.fetch, core_api['site_packages'])
            if response.error:
                self.write({'code': -1, 'msg': u'获取网站系统列表失败！'})
                self.finish()
                return
            else:
                packages = response.body
                with open(packages_cachefile, 'w') as f: f.write(packages)
        
        packages = tornado.escape.json_decode(packages)
        self.write({'code': 0, 'msg':'', 'data': packages})

        self.finish()

    def getdownloadtask(self):
        name = self.get_argument('name', '')
        version = self.get_argument('version', '')

        if not name or not version:
            self.write({'code': -1, 'msg': u'获取安装包下载地址失败！'})
            return

        # fetch package list from cache
        packages_cachefile = joinpath(self.settings['package_path'], '.meta')
        if not exists(packages_cachefile):
            self.write({'code': -1, 'msg': u'获取安装包下载地址失败！'})
            return
        with open(packages_cachefile) as f: packages = f.read()
        packages = tornado.escape.json_decode(packages)

        # check if name and version is available
        package = None
        for cate in packages:
            for pkg in cate['packages']:
                if pkg['code'] == name:
                    for v in pkg['versions']:
                        if v['code'] == version:
                            package = v
                            break
                if package: break
            if package: break
        if not package:
            self.write({'code': -1, 'msg': u'获取安装包下载地址失败！'})
            return

        filename = '%s-%s' % (name, version)
        workpath = joinpath(self.settings['package_path'], filename)
        if not exists(workpath): mkdir(workpath)

        filenameext = '%s%s' % (filename, package['ext'])
        filepath = joinpath(self.settings['package_path'], filenameext)

        self.write({'code': 0, 'msg': '', 'data': {
            'url': '%s&name=%s&version=%s' % (core_api['download_package'], name, version),
            'path': filepath,
            'temp': workpath,
        }})


class QueryHandler(RequestHandler):
    """Interface for querying server information.
    
    Query one or more items, seperated by comma.
    Examples:
    /query/*
    /query/server.*
    /query/service.*
    /query/server.datetime,server.diskinfo
    /query/config.fstab(sda1)
    """
    def get(self, items):
        self.authed()

        items = items.split(',')
        qdict = {'server': [], 'service': [], 'config': [], 'tool': []}
        for item in items:
            if item == '**':
                # query all items
                qdict = {'server': '**', 'service': '**'}
                break
            elif item == '*':
                # query all realtime update items
                qdict = {'server': '*', 'service': '*'}
                break
            elif item == 'server.**':
                qdict['server'] = '**'
            elif item == 'service.**':
                qdict['service'] = '**'
            else:
                item = _u(item)
                iteminfo = item.split('.', 1)
                if len(iteminfo) != 2: continue
                sec, q = iteminfo
                if sec not in ('server', 'service', 'config', 'tool'): continue
                if qdict[sec] == '**': continue
                qdict[sec].append(q)

        config_items = {
            'fstab'         : False,
        }
        tool_items = {
            'supportfs'     : False,
        }

        result = {}
        for sec, qs in qdict.items():
            if sec == 'server':
                server_items = ServerInfo.server_items
                if qs == '**':
                    qs = server_items.keys()
                elif qs == '*':
                    qs = [item for item, relup in server_items.items() if relup==True]
                for q in qs:
                    if q not in server_items: continue
                    result['%s.%s' % (sec, q)] = getattr(ServerInfo, q)()
            elif sec == 'service':
                service_items = Service.service_items
                autostart_services = Service.autostart_list()
                if qs == '**':
                    qs = service_items.keys()
                elif qs == '*':
                    qs = [item for item, relup in service_items.items() if relup==True]
                for q in qs:
                    if q not in service_items:
                        continue
                    status = Service.status(q)
                    result['%s.%s' % (sec, q)] = status and { 'status': status, 'autostart': q in autostart_services,} or None
            elif sec == 'config':
                for q in qs:
                    params = []
                    if q.endswith(')'):
                        q = q.strip(')').split('(', 1)
                        if len(q) != 2:
                            continue
                        q, params = q
                        params = params.split(',')
                    if not q in config_items:
                        continue
                    result['%s.%s' % (sec, q)] = getattr(ServerSet, q)(*params)
            elif sec == 'tool':
                for q in qs:
                    params = []
                    if q.endswith(')'):
                        q = q.strip(')').split('(', 1)
                        if len(q) != 2:
                            continue
                        q, params = q
                        params = params.split(',')
                    if not q in tool_items:
                        continue
                    result['%s.%s' % (sec, q)] = getattr(ServerTool, q)(*params)

        self.write(result)


class UtilsNetworkHandler(RequestHandler):
    """Handler for network ifconfig.
    """
    def get(self, sec, ifname):
        self.authed()
        if sec == 'hostname':
            self.write({'hostname': ServerInfo.hostname()})
        elif sec == 'ifnames':
            ifconfigs = ServerSet.ifconfigs()
            # filter lo
            del ifconfigs['lo']
            self.write({'ifnames': sorted(ifconfigs.keys())})
        elif sec == 'ifconfig':
            ifconfig = ServerSet.ifconfig(_u(ifname))
            if ifconfig != None: self.write(ifconfig)
        elif sec == 'nameservers':
            self.write({'nameservers': ServerSet.nameservers()})

    def post(self, sec, ifname):
        self.authed()
        if self.config.get('runtime', 'mode') == 'demo':
            self.write({'code': -1, 'msg': u'DEMO状态不允许修改网络设置！'})
            return

        if sec == 'hostname':
            hostname = self.get_argument('hostname', '')
            if hostname != '':
                if ServerSet.hostname(hostname):
                    self.write({'code': 0, 'msg': u'主机名保存成功！'})
                else:
                    self.write({'code': -1, 'msg': u'主机名保存失败！'})
            else:
                self.write({'code': -1, 'msg': u'主机名不能为空！'})

        if sec == 'ifconfig':
            ip = self.get_argument('ip', '')
            mask = self.get_argument('mask', '')
            gw = self.get_argument('gw', '')

            if not utils.is_valid_ip(_u(ip)):
                self.write({'code': -1, 'msg': u'%s 不是有效的IP地址！' % ip})
                return
            if not utils.is_valid_netmask(_u(mask)):
                self.write({'code': -1, 'msg': u'%s 不是有效的子网掩码！' % mask})
                return
            if gw != '' and not utils.is_valid_ip(_u(gw)):
                self.write({'code': -1, 'msg': u'网关IP %s 不是有效的IP地址！' % gw})
                return

            if ServerSet.ifconfig(_u(ifname), {'ip': _u(ip), 'mask': _u(mask), 'gw': _u(gw)}):
                self.write({'code': 0, 'msg': u'IP设置保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'IP设置保存失败！'})

        elif sec == 'nameservers':
            nameservers = _u(self.get_argument('nameservers', ''))
            nameservers = nameservers.split(',')

            for i, nameserver in enumerate(nameservers):
                if nameserver == '':
                    del nameservers[i]
                    continue
                if not utils.is_valid_ip(nameserver):
                    self.write({'code': -1, 'msg': u'%s 不是有效的IP地址！' % nameserver})
                    return

            if ServerSet.nameservers(nameservers):
                self.write({'code': 0, 'msg': u'DNS设置保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'DNS设置保存失败！'})

class UtilsTimeHandler(RequestHandler):
    """Handler for system datetime setting.
    """
    def get(self, sec, region=None):
        self.authed()
        if sec == 'datetime':
            self.write(ServerInfo.datetime(asstruct=True))
        elif sec == 'timezone':
            self.write({'timezone': ServerSet.timezone(self.inifile)})
        elif sec == 'timezone_list':
            if region == None:
                self.write({'regions': sorted(ServerSet.timezone_regions())})
            else:
                self.write({'cities': sorted(ServerSet.timezone_list(region))})

    def post(self, sec, ifname):
        self.authed()
        if self.config.get('runtime', 'mode') == 'demo':
            self.write({'code': -1, 'msg': u'DEMO 状态不允许时区设置！'})
            return

        if sec == 'timezone':
            timezone = self.get_argument('timezone', '')
            if ServerSet.timezone(self.inifile, _u(timezone)):
                self.write({'code': 0, 'msg': u'时区设置保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'时区设置保存失败！'})

class SettingHandler(RequestHandler):
    """Settings for InPanel
    """
    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, section):
        self.authed()
        if section == 'auth':
            username = self.config.get('auth', 'username')
            passwordcheck = self.config.getboolean('auth', 'passwordcheck')
            self.write({'username': username, 'passwordcheck': passwordcheck})
            self.finish()

        elif section == 'runtime':
            mode = self.config.get('runtime', 'mode')
            loginlockexpire = self.config.getint('runtime', 'loginlockexpire')
            loginfails = self.config.getint('runtime', 'loginfails')
            loginlock = self.config.getboolean('runtime', 'loginlock')
            if not mode:
                mode = 'prod'
                self.config.set('runtime', 'mode', 'prod')
            self.write({
                'mode': mode,
                'loginlockexpire': loginlockexpire,
                'loginfails': loginfails,
                'loginlock': loginlock
            })
            self.finish()

        elif section == 'server':
            ip = self.config.get('server', 'ip')
            port = self.config.get('server', 'port')
            forcehttps = self.config.getboolean('server', 'forcehttps')
            sslkey = self.config.get('server', 'sslkey')
            sslcrt = self.config.get('server', 'sslcrt')
            self.write({'forcehttps': forcehttps, 'ip': ip, 'port': port, 'sslkey': sslkey, 'sslcrt': sslcrt})
            self.finish()

        elif section == 'accesskey':
            accesskey = self.config.get('auth', 'accesskey')
            accesskeyenable = self.config.getboolean('auth', 'accesskeyenable')
            self.write({'accesskey': accesskey, 'accesskeyenable': accesskeyenable})
            self.finish()

        elif section == 'upver':
            force = self.get_argument('force', '')
            lastcheck = self.config.get('server', 'lastcheckupdate')
            lastcheck = 0 if lastcheck == '' else int(lastcheck)

            # detect new version daily
            if force or time.time() > lastcheck + 86400:
                http = tornado.httpclient.AsyncHTTPClient()
                response = yield tornado.gen.Task(utils.httpclient, core_api['latest'])
                # response = utils.httpclient(core_api['latest'])
                if response.error:
                    self.write({'code': -1, 'msg': u'获取新版本信息失败！'})
                else:
                    data = tornado.escape.json_decode(response.body)
                    self.write({'code': 0, 'msg':'', 'data': data})
                    self.config.set('server', 'lastcheckupdate', int(time.time()))
                    self.config.set('server', 'updateinfo', response.body)
            else:
                data = self.config.get('server', 'updateinfo')
                try:
                    data = tornado.escape.json_decode(data)
                except:
                    data = {}
                self.write({'code': 0, 'msg': '', 'data': data})

            self.finish()

    def post(self, section):
        self.authed()
        if section == 'auth':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许修改用户名和密码！'})
                return

            username = self.get_argument('username', '')
            password = self.get_argument('password', '')
            passwordc = self.get_argument('passwordc', '')
            passwordcheck = self.get_argument('passwordcheck', '')

            if username == '':
                self.write({'code': -1, 'msg': u'用户名不能为空！'})
                return
            if password != passwordc:
                self.write({'code': -1, 'msg': u'两次密码输入不一致！'})
                return

            if passwordcheck != 'on': passwordcheck = 'off'
            self.config.set('auth', 'passwordcheck', passwordcheck)

            if username != '':
                self.config.set('auth', 'username', username)
            if password != '':
                key = utils.randstr()
                pwd = hmac.new(key, password).hexdigest()
                self.config.set('auth', 'password', '%s:%s' % (pwd, key))

            self.write({'code': 0, 'msg': u'登录设置更新成功！'})

        elif section == 'server':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许修改服务绑定地址！'})
                return

            ip = self.get_argument('ip', '*')
            if ip != '*' and ip != '':
                if not utils.is_valid_ip(_u(ip)):
                    self.write({'code': -1, 'msg': u'%s 不是有效的IP地址！' % ip})
                    return
                netifaces = ServerInfo.netifaces()
                ips = [netiface['ip'] for netiface in netifaces]
                if not ip in ips:
                    msg = u'<p>%s 不是该服务器的IP地址！</p><p>可用的IP地址有：<br>%s</p>' % (ip, '<br>'.join(ips))
                    self.write({'code': -1, 'msg': msg})
                    return

            port = self.get_argument('port', '8888')
            port = int(port)
            if not port > 0 and port < 65535:
                self.write({'code': -1, 'msg': u'端口范围必须在 0 到 65535 之间！'})
                return

            self.config.set('server', 'ip', ip)
            self.config.set('server', 'port', port)

            sslkey = self.get_argument('sslkey', '')
            if sslkey == '' or exists(sslkey):
                self.config.set('server', 'sslkey', sslkey)
            else:
                self.write({'code': -1, 'msg': u'SSL私钥文件不存在，请仔细检查！'})
                return

            sslcrt = self.get_argument('sslcrt', '')
            if sslcrt == '' or exists(sslcrt):
                self.config.set('server', 'sslcrt', sslcrt)
            else:
                self.write({'code': -1, 'msg': u'SSL证书文件不存在，请仔细检查！'})
                return

            forcehttps = self.get_argument('forcehttps', '')
            if forcehttps == 'on':
                if not exists(sslkey):
                    self.config.set('server', 'forcehttps', 'off')
                    self.write({'code': -1, 'msg': u'请填写SSL私钥文件！'})
                    return
                elif not exists(sslcrt):
                    self.config.set('server', 'forcehttps', 'off')
                    self.write({'code': -1, 'msg': u'请填写SSL证书文件！'})
                    return
                self.config.set('server', 'forcehttps', forcehttps)
            else:
                self.config.set('server', 'forcehttps', 'off')

            self.write({'code': 0, 'msg': u'服务设置更新成功！将在重启服务后生效。'})

        elif section == 'accesskey':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许修改远程控制设置！'})
                return

            accesskey = self.get_argument('accesskey', '')
            accesskeyenable = self.get_argument('accesskeyenable', '')

            if accesskeyenable == 'on' and accesskey == '':
                self.write({'code': -1, 'msg': u'远程控制密钥不能为空！'})
                return

            if accesskey != '':
                try:
                    if len(b64decode(accesskey)) != 32:
                        raise Exception()
                except:
                    self.write({'code': -1, 'msg': u'远程控制密钥格式不正确！'})
                    return

            if accesskeyenable != 'on': accesskeyenable = 'off'
            self.config.set('auth', 'accesskeyenable', accesskeyenable)
            self.config.set('auth', 'accesskey', accesskey)

            self.write({'code': 0, 'msg': u'远程控制设置更新成功！'})


class OperationHandler(RequestHandler):
    ''''Server operation handler
    '''

    def post(self, op):
        """Run a server operation
        """
        self.authed()
        if hasattr(self, op):
            getattr(self, op)()
        else:
            self.write({'code': -1, 'msg': u'未定义的操作！'})

    def reboot(self):
        if self.config.get('runtime', 'mode') == 'demo':
            self.write({'code': -1, 'msg': u'DEMO状态不允许重启服务器！'})
            return

        p = Popen('reboot', stdout=PIPE, stderr=PIPE, close_fds=True)
        info = p.stdout.read()
        p.stderr.read()
        if p.wait() == 0:
            self.write({'code': 0, 'msg': u'已向系统发送重启指令，系统即将重启！'})
        else:
            self.write({'code': -1, 'msg': u'向系统发送重启指令失败！'})

    def fdisk(self):
        action = self.get_argument('action', '')
        devname = self.get_argument('devname', '')

        if action == 'add':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许添加分区！'})
                return

            size = self.get_argument('size', '')
            unit = self.get_argument('unit', '')

            if unit not in ('M', 'G'):
                self.write({'code': -1, 'msg': u'错误的分区大小！'})
                return

            if size == '':
                size = None # use whole left space
            else:
                try:
                    size = float(size)
                except:
                    self.write({'code': -1, 'msg': u'错误的分区大小！'})
                    return

                if unit == 'G' and size-int(size) > 0:
                    size *= 1024
                    unit = 'M'
                size = '%d%s' % (round(size), unit)

            if fdisk.add('/dev/%s' % _u(devname), _u(size)):
                self.write({'code': 0, 'msg': u'在 %s 设备上创建分区成功！' % devname})
            else:
                self.write({'code': -1, 'msg': u'在 %s 设备上创建分区失败！' % devname})

        elif action == 'delete':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许删除分区！'})
                return

            if fdisk.delete('/dev/%s' % _u(devname)):
                # remove config from /etc/fstab
                ServerSet.fstab(_u(devname), {
                    'devname': _u(devname),
                    'mount': None,
                })
                self.write({'code': 0, 'msg': u'分区 %s 删除成功！' % devname})
            else:
                self.write({'code': -1, 'msg': u'分区 %s 删除失败！' % devname})

        elif action == 'scan':
            if fdisk.scan('/dev/%s' % _u(devname)):
                self.write({'code': 0, 'msg': u'扫描设备 %s 的分区成功！' % devname})
            else:
                self.write({'code': -1, 'msg': u'扫描设备 %s 的分区失败！' % devname})

        else:
            self.write({'code': -1, 'msg': u'未定义的操作！'})

    def chkconfig(self):
        name = self.get_argument('name', '')
        service = self.get_argument('service', '')
        autostart = self.get_argument('autostart', '')
        if not name: name = service

        autostart_str = {'on': u'启用', 'off': u'禁用'}
        if Service.autostart_set(_u(service), autostart == 'on' and True or False):
            self.write({'code': 0, 'msg': u'成功%s %s 自动启动！' % (autostart_str[autostart], name)})
        else:
            self.write({'code': -1, 'msg': u'%s %s 自动启动失败！' % (autostart_str[autostart], name)})

    def user(self):
        action = self.get_argument('action', '')

        if action == 'listuser':
            fullinfo = self.get_argument('fullinfo', 'on')
            self.write({'code': 0, 'msg': u'成功获取用户列表！', 'data': user.listuser(fullinfo=='on')})

        elif action == 'listgroup':
            fullinfo = self.get_argument('fullinfo', 'on')
            self.write({'code': 0, 'msg': u'成功获取用户组列表！', 'data': user.listgroup(fullinfo=='on')})

        elif action in ('useradd', 'usermod'):
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许添加和修改用户！'})
                return

            pw_name = self.get_argument('pw_name', '')
            pw_gecos = self.get_argument('pw_gecos', '')
            pw_gname = self.get_argument('pw_gname', '')
            pw_dir = self.get_argument('pw_dir', '')
            pw_shell = self.get_argument('pw_shell', '')
            pw_passwd = self.get_argument('pw_passwd', '')
            pw_passwdc = self.get_argument('pw_passwdc', '')
            lock = self.get_argument('lock', '')
            lock = (lock == 'on') and True or False
            
            if pw_passwd != pw_passwdc:
                self.write({'code': -1, 'msg': u'两次输入的密码不一致！'})
                return
            
            options = {
                'pw_gecos': _u(pw_gecos),
                'pw_gname': _u(pw_gname),
                'pw_dir': _u(pw_dir),
                'pw_shell': _u(pw_shell),
                'lock': lock
            }
            if len(pw_passwd)>0: options['pw_passwd'] = _u(pw_passwd)

            if action == 'useradd':
                createhome = self.get_argument('createhome', '')
                createhome = (createhome == 'on') and True or False
                options['createhome'] = createhome
                if user.useradd(_u(pw_name), options):
                    self.write({'code': 0, 'msg': u'用户添加成功！'})
                else:
                    self.write({'code': -1, 'msg': u'用户添加失败！'})
            elif action == 'usermod':
                if user.usermod(_u(pw_name), options):
                    self.write({'code': 0, 'msg': u'用户修改成功！'})
                else:
                    self.write({'code': -1, 'msg': u'用户修改失败！'})

        elif action == 'userdel':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许删除用户！'})
                return

            pw_name = self.get_argument('pw_name', '')
            if user.userdel(_u(pw_name)):
                self.write({'code': 0, 'msg': u'用户删除成功！'})
            else:
                self.write({'code': -1, 'msg': u'用户删除失败！'})

        elif action in ('groupadd', 'groupmod', 'groupdel'):
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许操作用户组！'})
                return

            gr_name = self.get_argument('gr_name', '')
            gr_newname = self.get_argument('gr_newname', '')
            actionstr = {'groupadd': u'添加', 'groupmod': u'修改', 'groupdel': u'删除'}

            if action == 'groupmod':
                rt = user.groupmod(_u(gr_name), _u(gr_newname))
            else:
                rt = getattr(user, action)(_u(gr_name))
            if rt:
                self.write({'code': 0, 'msg': u'用户组%s成功！' % actionstr[action]})
            else:
                self.write({'code': -1, 'msg': u'用户组%s失败！' % actionstr[action]})

        elif action in ('groupmems_add', 'groupmems_del'):
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许操作用户组成员！'})
                return

            gr_name = self.get_argument('gr_name', '')
            mem = self.get_argument('mem', '')
            option = action.split('_')[1]
            optionstr = {'add': u'添加', 'del': u'删除'}
            if user.groupmems(_u(gr_name), _u(option), _u(mem)):
                self.write({'code': 0, 'msg': u'用户组成员%s成功！' % optionstr[option]})
            else:
                self.write({'code': -1, 'msg': u'用户组成员%s成功！' % optionstr[option]})

    def file(self):
        action = self.get_argument('action', '')

        if action == 'last':
            lastdir = self.config.get('file', 'lastdir')
            lastfile = self.config.get('file', 'lastfile')
            self.write({'code': 0, 'msg': '', 'data': {'lastdir': lastdir, 'lastfile': lastfile}})

        elif action == 'listdir':
            path = self.get_argument('path', '')
            showhidden = self.get_argument('showhidden', 'off')
            remember = self.get_argument('remember', 'on')
            onlydir = self.get_argument('onlydir', 'off')
            items = files.listdir(_u(path), showhidden=='on', onlydir=='on')
            if items == False:
                self.write({'code': -1, 'msg': u'目录 %s 不存在！' % path})
            else:
                if remember == 'on': self.config.set('file', 'lastdir', path)
                self.write({'code': 0, 'msg': u'成功获取文件列表！', 'data': items})

        elif action == 'getitem':
            path = self.get_argument('path', '')
            item = files.getitem(_u(path))
            if item == False:
                self.write({'code': -1, 'msg': u'%s 不存在！' % path})
            else:
                self.write({'code': 0, 'msg': u'成功获取 %s 的信息！' % path, 'data': item})

        elif action == 'fread':
            path = self.get_argument('path', '')
            remember = self.get_argument('remember', 'on')
            size = files.fsize(_u(path))
            if size == None:
                self.write({'code': -1, 'msg': u'文件 %s 不存在！' % path})
            elif size > 1024*1024: # support 1MB of file at max
                self.write({'code': -1, 'msg': u'读取 %s 失败！不允许在线编辑超过1MB的文件！' % path})
            elif not files.istext(_u(path)):
                self.write({'code': -1, 'msg': u'读取 %s 失败！无法识别文件类型！' % path})
            else:
                if remember == 'on': self.config.set('file', 'lastfile', path)
                with open(path) as f: content = f.read()
                charset, content = files.decode(content)
                if not charset:
                    self.write({'code': -1, 'msg': u'不可识别的文件编码！'})
                    return
                data = {
                    'filename': basename(path),
                    'filepath': path,
                    'mimetype': files.mimetype(_u(path)),
                    'charset': charset,
                    'content': content,
                }
                self.write({'code': 0, 'msg': u'成功读取文件内容！', 'data': data})

        elif action == 'fclose':
            self.config.set('file', 'lastfile', '')
            self.write({'code': 0, 'msg': ''})

        elif action == 'fwrite':
            path = self.get_argument('path', '')
            charset = self.get_argument('charset', '')
            content = self.get_argument('content', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                    return

            if not charset in files.charsets:
                self.write({'code': -1, 'msg': u'不可识别的文件编码！'})
                return
            content = files.encode(content, charset)
            if not content:
                self.write({'code': -1, 'msg': u'文件编码转换出错，保存失败！'})
                return
            if files.fsave(_u(path), content):
                self.write({'code': 0, 'msg': u'文件保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'文件保存失败！'})

        elif action == 'createfolder':
            path = self.get_argument('path', '')
            name = self.get_argument('name', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www') and not path.startswith(self.settings['package_path']):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                    return

            if files.dadd(_u(path), _u(name)):
                self.write({'code': 0, 'msg': u'文件夹创建成功！'})
            else:
                self.write({'code': -1, 'msg': u'文件夹创建失败！'})

        elif action == 'createfile':
            path = self.get_argument('path', '')
            name = self.get_argument('name', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                    return

            if files.fadd(_u(path), _u(name)):
                self.write({'code': 0, 'msg': u'文件创建成功！'})
            else:
                self.write({'code': -1, 'msg': u'文件创建失败！'})

        elif action == 'rename':
            path = self.get_argument('path', '')
            name = self.get_argument('name', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                    return

            if files.rename(_u(path), _u(name)):
                self.write({'code': 0, 'msg': u'重命名成功！'})
            else:
                self.write({'code': -1, 'msg': u'重命名失败！'})

        elif action == 'exist':
            path = self.get_argument('path', '')
            name = self.get_argument('name', '')
            self.write({'code': 0, 'msg': '', 'data': exists(joinpath(path, name))})

        elif action == 'link':
            srcpath = self.get_argument('srcpath', '')
            despath = self.get_argument('despath', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not despath.startswith('/var/www') and not despath.startswith(self.settings['package_path']):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许在除 /var/www 以外的目录下创建链接！'})
                    return

            if files.link(_u(srcpath), _u(despath)):
                self.write({'code': 0, 'msg': u'链接 %s 创建成功！' % despath})
            else:
                self.write({'code': -1, 'msg': u'链接 %s 创建失败！' % despath})

        elif action == 'delete':
            paths = self.get_argument('paths', '')
            paths = paths.split(',')

            if self.config.get('runtime', 'mode') == 'demo':
                for path in paths:
                    if not path.startswith('/var/www') and not path.startswith(self.settings['package_path']):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许在除 /var/www 以外的目录执行删除操作！'})
                        return

            if len(paths) == 1:
                path = paths[0]
                if files.delete(_u(path)):
                    self.write({'code': 0, 'msg': u'已将 %s 移入回收站！' % path})
                else:
                    self.write({'code': -1, 'msg': u'将 %s 移入回收站失败！' % path})
            else:
                for path in paths:
                    if not files.delete(_u(path)):
                        self.write({'code': -1, 'msg': u'将 %s 移入回收站失败！' % path})
                        return
                self.write({'code': 0, 'msg': u'批量移入回收站成功！'})

        elif action == 'tlist':
            self.write({'code': 0, 'msg': '', 'data': files.tlist()})

        elif action == 'trashs':
            self.write({'code': 0, 'msg': '', 'data': files.trashs()})

        elif action == 'titem':
            mount = self.get_argument('mount', '')
            uuid = self.get_argument('uuid', '')
            info = files.titem(_u(mount), _u(uuid))
            if info:
                self.write({'code': 0, 'msg': '', 'data': info})
            else:
                self.write({'code': -1, 'msg': '获取项目信息失败！'})

        elif action == 'trestore':
            mount = self.get_argument('mount', '')
            uuid = self.get_argument('uuid', '')
            info = files.titem(_u(mount), _u(uuid))
            if info and files.trestore(_u(mount), _u(uuid)):
                self.write({'code': 0, 'msg': u'已还原 %s 到 %s！' % \
                    (_d(info['name']), _d(info['path']))})
            else:
                self.write({'code': -1, 'msg': u'还原失败！'})

        elif action == 'tdelete':
            mount = self.get_argument('mount', '')
            uuid = self.get_argument('uuid', '')
            info = files.titem(_u(mount), _u(uuid))
            if info and files.tdelete(_u(mount), _u(uuid)):
                self.write({'code': 0, 'msg': u'已删除 %s！' % _d(info['name'])})
            else:
                self.write({'code': -1, 'msg': u'删除失败！'})

    def apache(self):
        action = self.get_argument('action', '')
        if action == 'getservers':
            sites = apache.getservers()
            self.write({'code': 0, 'msg': '', 'data': sites})

        elif action in ('enableserver', 'disableserver', 'deleteserver'):
            ip = self.get_argument('ip', '')
            port = self.get_argument('port', '')
            name = self.get_argument('server_name', '')
            handler = getattr(apache, action)
            opstr = {
                'enableserver': u'启用',
                'disableserver': u'停用',
                'deleteserver': u'删除',
            }
            if handler(name, ip, port):
                self.write({'code': 0, 'msg': u'站点 %s:%s %s成功！' % (name, port, opstr[action])})
            else:
                self.write({'code': -1, 'msg': u'站点 %s:%s %s失败！' % (name, port, opstr[action])})

        elif action == 'get_settings':
            # items = self.get_argument('items', '')
            # items = items.split(',')
            config = apache.loadconfig()
            self.write({'code': 0, 'msg': '', 'data': config})

        elif action == 'getserver':
            ip = self.get_argument('ip', '')
            port = self.get_argument('port', '')
            name = self.get_argument('name', '')
            serverinfo = apache.getserver(_u(ip), _u(port), _u(name))
            if serverinfo:
                self.write({'code': 0, 'msg': u'站点信息读取成功！', 'data': serverinfo})
            else:
                self.write({'code': -1, 'msg': u'站点不存在！'})

        elif action in ('addserver', 'updateserver'):
            setting = loads(self.get_argument('setting', '')) or {}

            ip = setting.get('ip', '')
            if ip not in ('', '*', '0.0.0.0') and not utils.is_valid_ip(ip):
                self.write({'code': -1, 'msg': u'%s 不是有效的IP地址！' % ip})
                return

            port = int(setting.get('port', 0))
            if port <= 0 or port > 65535:
                self.write({'code': -1, 'msg': u'%s 不是有效的端口号!' % setting.get('port')})
                return

            servername = setting.get('servername')
            print('servername', servername)
            if not utils.is_valid_domain(servername):
                self.write({'code': -1, 'msg': u'%s 不是有效的域名！' % servername})
                return

            documentroot = setting.get('documentroot', '')
            if not documentroot:
                self.write({'code': -1, 'msg': u'%s 不是有效的目录！' % documentroot})
                return
            autocreate = setting.get('autocreate')
            if not exists(documentroot):
                if autocreate:
                    try:
                        mkdir(documentroot)
                    except:
                        self.write({'code': -1, 'msg': u'站点目录 %s 创建失败！' % documentroot})
                        return
                else:
                    self.write({'code': -1, 'msg': u'站点目录 %s 不存在！' % documentroot})
                    return

            directoryindex = setting.get('directoryindex')
            serveralias = setting.get('serveralias')
            serveradmin = setting.get('serveradmin')
            errorlog = setting.get('errorlog')
            customlog = setting.get('customlog')
            directory = setting.get('directory')

            version = self.get_argument('version', '')  # apache version
            for diret in directory:
                if 'path' in diret and diret['path']:
                    if not exists(diret['path']) and 'autocreate' in diret and diret['autocreate']:
                        try:
                            mkdir(diret['path'])
                        except:
                            self.write({'code': -1, 'msg': u'路径 %s 创建失败！' % diret['path']})
                            return
                else:
                    self.write({'code': -1, 'msg': u'请选择路径！'})
                    return
            if action == 'addserver':
                if not apache.addserver(servername, ip, port, serveralias=serveralias, serveradmin=serveradmin, documentroot=documentroot, directoryindex=directoryindex, directory=directory,
                    errorlog=errorlog, customlog=customlog, version=version):
                    self.write({'code': -1, 'msg': u'新站点添加失败！请检查站点域名是否重复。', 'data': setting})
                else:
                    self.write({'code': 0, 'msg': u'新站点添加成功！', 'data': setting})
            else:
                c_ip = _u(self.get_argument('ip', ''))
                c_port = _u(self.get_argument('port', ''))
                c_name = _u(self.get_argument('name', ''))
                if not apache.updateserver(c_name, c_ip, c_port, serveralias=serveralias, serveradmin=serveradmin, documentroot=documentroot, directoryindex=directoryindex, directory=directory,
                    errorlog=errorlog, customlog=customlog, version=version):
                    self.write({'code': -1, 'msg': u'站点设置更新失败！请检查配置信息（如域名是否重复？）', 'data': setting})
                else:
                    self.write({'code': 0, 'msg': u'站点设置更新成功！', 'data': setting})

    def nginx(self):
        action = self.get_argument('action', '')

        if action == 'getservers':
            sites = nginx.getservers()
            self.write({'code': 0, 'msg': '', 'data': sites})

        elif action in ('enableserver', 'disableserver', 'deleteserver'):
            ip = self.get_argument('ip', '')
            port = self.get_argument('port', '')
            name = self.get_argument('server_name', '')
            handler = getattr(nginx, action)
            opstr = {
                'enableserver': u'启用',
                'disableserver': u'停用',
                'deleteserver': u'删除',
            }
            if handler(ip, port, name):
                self.write({'code': 0, 'msg': u'站点 %s:%s %s成功！' % (name, port, opstr[action])})
            else:
                self.write({'code': -1, 'msg': u'站点 %s:%s %s失败！' % (name, port, opstr[action])})

        elif action == 'gethttpsettings':
            items = self.get_argument('items', '')
            items = items.split(',')

            if 'limit_conn_zone' in items:
                items.append('limit_zone') # version < 1.1.8

            data = {}
            config = nginx.loadconfig()
            for item in items:
                if item.endswith('[]'):
                    item = item[:-2]
                    returnlist = True
                    values = nginx.http_get(_u(item), config)
                else:
                    returnlist = False
                    values = [nginx.http_getfirst(_u(item), config)]
                
                if values:
                    if item == 'gzip':
                        # eg. gzip off
                        values = [v=='on' for v in values if v]
                    elif item == 'limit_rate':
                        # eg. limit_rate 100k
                        values = [v.replace('k', '') for v in values if v]
                    elif item == 'limit_conn':
                        # eg. limit_conn  one  1
                        values = [v.split()[-1] for v in values if v]
                    elif item == 'limit_conn_zone':
                        # eg. limit_conn_zone $binary_remote_addr  zone=addr:10m
                        values = [v.split(':')[-1].replace('m', '') for v in values if v]
                    elif item == 'limit_zone': # version < 1.1.8
                        # eg. limit_zone addr $binary_remote_addr 10m
                        values = [v.split()[-1].replace('m', '') for v in values if v]
                    elif item == 'client_max_body_size':
                        # eg. client_max_body_size 1m
                        values = [v.replace('m', '') for v in values if v]
                    elif item == 'keepalive_timeout':
                        # eg. keepalive_timeout 75s
                        values = [v.replace('s', '') for v in values if v]
                    elif item == 'allow':
                        # allow all
                        values = [v for v in values if v and v!='all']
                    elif item == 'deny':
                        # deny all
                        values = [v for v in values if v and v!='all']
                    elif item == 'proxy_cache_path':
                        # eg. levels=1:2 keys_zone=newcache:10m inactive=10m max_size=100m
                        result = []
                        for v in values:
                            info = {}
                            fields = v.split()
                            info['path'] = fields[0]
                            for field in fields[1:]:
                                key, value = field.split('=', 1)
                                if key == 'levels':
                                    levels = value.split(':')
                                    info['path_level_1'] = levels[0]
                                    if len(levels) > 1: info['path_level_2'] = levels[1]
                                    if len(levels) > 2: info['path_level_3'] = levels[2]
                                elif key == 'keys_zone':
                                    t = value.split(':')
                                    info['name'] = t[0]
                                    if len(t) > 1: info['mem'] = t[1].replace('m', '')
                                elif key == 'inactive':
                                    info['inactive'] = value[:-1]
                                    info['inactive_unit'] = value[-1]
                                elif key == 'max_size':
                                    info['max_size'] = value[:-1]
                                    info['max_size_unit'] = value[-1]
                            result.append(info)
                        values = result

                if item == 'limit_zone':
                    item = 'limit_conn_zone' # version < 1.1.8

                if returnlist:
                    data[item] = values
                else:
                    data[item] = values and values[0] or ''
            self.write({'code': 0, 'msg': '', 'data': data})

        elif action == 'sethttpsettings':
            version = self.get_argument('version', '')
            gzip = self.get_argument('gzip', '')
            limit_rate = self.get_argument('limit_rate', '')
            limit_conn = self.get_argument('limit_conn', '')
            limit_conn_zone = self.get_argument('limit_conn_zone', '')
            client_max_body_size = self.get_argument('client_max_body_size', '')
            keepalive_timeout = self.get_argument('keepalive_timeout', '')
            allow = self.get_argument('allow', '')
            deny = self.get_argument('deny', '')
            access_status = self.get_argument('access_status', '')

            setting = {}
            setting['gzip'] = gzip=='on' and 'on' or 'off'
            if not limit_rate.isdigit(): limit_rate = ''
            setting['limit_rate'] = limit_rate and '%sk' % limit_rate or ''
            if not limit_conn.isdigit(): limit_conn = ''
            setting['limit_conn'] = limit_conn and 'addr %s' % limit_conn or ''
            if not limit_conn_zone.isdigit(): limit_conn_zone = '10'
            if not version or utils.version_get(version, '1.1.8'):
                setting['limit_conn_zone'] = '$binary_remote_addr zone=addr:%sm' % limit_conn_zone
                setting['limit_zone'] = ''
            else:
                setting['limit_zone'] = 'addr $binary_remote_addr %sm' % limit_conn_zone
                setting['limit_conn_zone'] = ''
            if not client_max_body_size.isdigit(): client_max_body_size = '1'
            setting['client_max_body_size'] = '%sm' % client_max_body_size
            if not keepalive_timeout.isdigit(): keepalive_timeout = ''
            setting['keepalive_timeout'] = keepalive_timeout and '%ss' % keepalive_timeout or ''
            if access_status == 'white':
                setting['allow'] = [a.strip() for a in allow.split() if a.strip()]
                setting['deny'] = 'all'
            elif access_status == 'black':
                setting['deny'] = [a.strip() for a in deny.split() if a.strip()]
                setting['allow'] = ''
            else:
                setting['allow'] = setting['deny'] = ''

            directives = ('gzip', 'limit_rate', 'limit_conn', 'limit_conn_zone', 'limit_zone',
                    'client_max_body_size', 'keepalive_timeout', 'allow', 'deny')
            for directive in directives:
                if not directive in setting: continue
                value = setting[directive]
                if isinstance(value, unicode):
                    value = _u(value)
                elif isinstance(value, list):
                    for i,v in enumerate(value):
                        value[i] = _u(v)
                nginx.http_set(directive, value)

            self.write({'code': 0, 'msg': u'设置保存成功！'})

        elif action == 'setproxycachesettings':
            proxy_caches = tornado.escape.json_decode(self.get_argument('proxy_caches', ''))

            values = []
            for cache in proxy_caches:
                fields = []
                if 'path' in cache and cache['path']:
                    if not exists(cache['path']) and 'autocreate' in cache and cache['autocreate']:
                        try:
                            mkdir(cache['path'])
                        except:
                            self.write({'code': -1, 'msg': u'缓存目录 %s 创建失败！' % cache['path']})
                            return
                else:
                    self.write({'code': -1, 'msg': u'请选择缓存目录！'})
                    return
                fields.append(cache['path'])
                if not 'path_level_1' in cache or not cache['path_level_1'].isdigit() or \
                   not 'path_level_2' in cache or not cache['path_level_2'].isdigit() or \
                   not 'path_level_3' in cache or not cache['path_level_3'].isdigit():
                    self.write({'code': -1, 'msg': u'缓存目录名长度必须是数字！'})
                    return
                if int(cache['path_level_1']) + int(cache['path_level_2']) + int(cache['path_level_3']) > 32:
                    self.write({'code': -1, 'msg': u'缓存目录名长度总和不能超过32位！'})
                    return
                levels = [cache['path_level_1']]
                if int(cache['path_level_2']) > 0: levels.append(cache['path_level_2'])
                if int(cache['path_level_3']) > 0: levels.append(cache['path_level_3'])
                fields.append('levels=%s' % (':'.join(levels)))

                if not 'name' in cache or cache['name'].strip() == '':
                    self.write({'code': -1, 'msg': u'缓存区名称不能为空！'})
                    return
                if not 'mem' in cache or not cache['mem'].isdigit():
                    self.write({'code': -1, 'msg': u'缓存计数内存大小必须是数字！'})
                    return
                fields.append('keys_zone=%s:%sm' % (cache['name'].strip(), cache['mem']))

                if not 'inactive' in cache or not cache['inactive'].isdigit():
                    self.write({'code': -1, 'msg': u'缓存过期时间必须是数字！'})
                    return
                if not 'inactive_unit' in cache or not cache['inactive_unit'] in ('s', 'm', 'h', 'd'):
                    self.write({'code': -1, 'msg': u'缓存过期时间单位错误！'})
                    return
                fields.append('inactive=%s%s' % (cache['inactive'], cache['inactive_unit']))

                if not 'max_size' in cache or not cache['max_size'].isdigit():
                    self.write({'code': -1, 'msg': u'缓存大小限制值必须是数字！'})
                    return
                if not 'max_size_unit' in cache or not cache['max_size_unit'] in ('m', 'g'):
                    self.write({'code': -1, 'msg': u'缓存大小限制单位错误！'})
                    return
                fields.append('max_size=%s%s' % (cache['max_size'], cache['max_size_unit']))

                values.append(' '.join(fields))

            nginx.http_set('proxy_cache_path', values)            
            self.write({'code': 0, 'msg': u'设置保存成功！'})

        elif action == 'getserver':
            ip = self.get_argument('ip', '')
            port = self.get_argument('port', '')
            server_name = self.get_argument('server_name', '')
            serverinfo = nginx.getserver(_u(ip), _u(port), _u(server_name))
            if serverinfo:
                self.write({'code': 0, 'msg': u'站点信息读取成功！', 'data': serverinfo})
            else:
                self.write({'code': -1, 'msg': u'站点不存在！'})

        elif action in ('addserver', 'updateserver'):
            if action == 'updateserver':
                old_server_ip = self.get_argument('ip', '')
                old_server_port = self.get_argument('port', '')
                old_server_name = self.get_argument('server_name', '')

            version = self.get_argument('version', '')  # nginx version
            setting = tornado.escape.json_decode(self.get_argument('setting', ''))

            #import pprint
            #pp = pprint.PrettyPrinter(indent=4)
            #pp.pprint(setting)

            # validate server name
            server_names = None
            if 'server_names' in setting:
                server_names = [s['name'].strip().lower() for s in setting['server_names'] if s['name'].strip()]
                for server_name in server_names:
                    if server_name != '_' and not utils.is_valid_domain(_u(server_name)):
                        server_names = None
                        break
            if not server_names:
                self.write({'code': -1, 'msg': u'请输入有效的站点域名！'})
                return

            # validate listens
            listens = None
            if 'listens' in setting:
                listens = setting['listens']
                ipportpairs = []
                for listen in listens:
                    if 'ip' in listen:
                        if listen['ip'] not in ('', '*', '0.0.0.0') and not utils.is_valid_ip(_u(listen['ip'])):
                            listens = None
                            break
                    if not 'port' in listen:
                        listens = None
                        break
                    elif not listen['port'].isdigit():
                        listens = None
                        break
                    else:
                        port = int(listen['port'])
                        if port <= 0 or port > 65535:
                            listens = None
                            break
                    ipport = '%s:%s' % (listen['ip'], listen['port'])
                    if ipport in ipportpairs:
                        self.write({'code': -1, 'msg': u'监听的IP:端口重复！'})
                        return
                    if listen['ip'] in ('', '*', '0.0.0.0'):
                        ipportpairs.append(ipport)
            if not listens:
                self.write({'code': -1, 'msg': u'请输入有效的监听地址！'})
                return

            # validate charset
            charset = None
            charsets = ('', 'utf-8', 'gb2312', 'gbk', 'gb18030',
                'big5', 'euc-jp', 'euc-kr', 'iso-8859-2', 'shift_jis')
            if 'charset' in setting:
                charset = setting['charset']
                if not charset in charsets:
                    self.write({'code': -1, 'msg': u'请选择有效的字符编码！'})
                    return

            # skip validate index
            if 'index' in setting:
                index = setting['index']

            # validate limit_rate
            limit_rate = None
            if 'limit_rate' in setting:
                limit_rate = setting['limit_rate']
                if not limit_rate == '' and not limit_rate.isdigit():
                    self.write({'code': -1, 'msg': u'下载速度限制必须为数字！'})
                    return

            # validate limit_conn
            limit_conn = None
            if 'limit_conn' in setting:
                limit_conn = setting['limit_conn']
                if not limit_conn == '' and not limit_conn.isdigit():
                    self.write({'code': -1, 'msg': u'连接数限制必须为数字！'})
                    return

            # validate ssl_crt and ssl_key
            ssl_crt = ssl_key = None
            if 'ssl_crt' in setting and 'ssl_key' in setting:
                if setting['ssl_crt'] or setting['ssl_key']:
                    ssl_crt = setting['ssl_crt']
                    ssl_key = setting['ssl_key']
                    if not exists(ssl_crt) or not exists(ssl_key):
                        self.write({'code': -1, 'msg': u'SSL证书或密钥不存在！'})
                        return

            # validate rewrite_rules
            rewrite_rules = None
            if 'rewrite_enable' in setting and setting['rewrite_enable']:
                if 'rewrite_rules' in setting:
                    rules = setting['rewrite_rules'].split('\n')
                    rewrite_rules = []
                    for rule in rules:
                        rule = rule.strip().strip(';')
                        if rule == '': continue
                        t = re.split(r'\s+', rule)
                        #if not re.match(r'^rewrite\s+.+\s+(?:last|break|redirect|permanent);?$', rule):
                        if len(t) not in (3, 4) or \
                           len(t) == 4 and (t[0] != 'rewrite' or t[-1] not in ('last', 'break', 'redirect', 'permanent')) or \
                           len(t) == 3 and t[0] != 'rewrite':
                            self.write({'code': -1, 'msg': u'Rewrite 规则 “%s” 格式有误！' % rule})
                            return
                        rewrite_rules.append(rule)

            # validate locations
            locations = []
            urlpaths = []
            if 'locations' in setting:
                locs = setting['locations']
                for loc in locs:
                    if not 'urlpath' in loc:
                        self.write({'code': -1, 'msg': u'站点URL路径输入错误！'})
                        return
                    if not 'engine' in loc \
                        or loc['engine'] not in ('static', 'fastcgi', 'redirect', 'proxy', 'error'):
                        self.write({'code': -1, 'msg': u'站点路径引擎选择存在错误！'})
                        return
                    if not loc['engine'] in loc:
                        self.write({'code': -1, 'msg': u'缺少站点路径配置！'})
                        return
                    location = {}
                    location['urlpath'] = loc['urlpath']
                    if loc['urlpath'] in urlpaths:
                        self.write({'code': -1, 'msg': u'重复的站点路径 %s！' % loc['urlpath']})
                        return
                    urlpaths.append(loc['urlpath'])
                    locsetting = loc[loc['engine']]
                    if loc['engine'] in ('static', 'fastcgi'):
                        if not 'root' in locsetting:
                            self.write({'code': -1, 'msg': u'站点目录不能为空！' % locsetting['root']})
                            return
                        if not exists(locsetting['root']):
                            if 'autocreate' in locsetting and locsetting['autocreate']:
                                try:
                                    mkdir(locsetting['root'])
                                except:
                                    self.write({'code': -1, 'msg': u'站点目录 %s 创建失败！' % locsetting['root']})
                                    return
                            else:
                                self.write({'code': -1, 'msg': u'站点目录 %s 不存在！' % locsetting['root']})
                                return
                        location['root'] = locsetting['root']
                        if 'charset' in locsetting and locsetting['charset'] in charsets:
                            location['charset'] = locsetting['charset']
                        if 'index' in locsetting:
                            location['index'] = locsetting['index']
                        if 'rewrite_enable' in locsetting and locsetting['rewrite_enable']:
                            if 'rewrite_detect_file' in locsetting and locsetting['rewrite_detect_file']:
                                location['rewrite_detect_file'] = True
                            else:
                                location['rewrite_detect_file'] = False
                            location['rewrite_rules'] = []
                            rwrules = locsetting['rewrite_rules'].split('\n')
                            for rule in rwrules:
                                rule = rule.strip().strip(';')
                                if rule == '': continue
                                t = re.split('\s+', rule)
                                if len(t) not in (3, 4) or \
                                   len(t) == 4 and (t[0] != 'rewrite' or t[-1] not in ('last', 'break')) or \
                                   len(t) == 3 and t[0] != 'rewrite':
                                    self.write({'code': -1, 'msg': u'Rewrite 规则 “%s” 格式有误！' % rule})
                                    return
                                location['rewrite_rules'].append(rule)
                    if loc['engine'] == 'static':
                        if 'autoindex' in locsetting and locsetting['autoindex']:
                            location['autoindex'] = True
                    elif loc['engine'] == 'fastcgi':
                        if not 'fastcgi_pass' in locsetting or not locsetting['fastcgi_pass']:
                            self.write({'code': -1, 'msg': u'请输入FastCGI服务器地址！'})
                            return
                        fastcgi_pass = locsetting['fastcgi_pass']
                        if not fastcgi_pass.startswith('unix:'):
                            fields = fastcgi_pass.split(':', 1)
                            if len(fields) > 1:
                                server, port = fields
                            else:
                                server = fields[0]
                                port = None
                            if not utils.is_valid_domain(_u(server)) or port and not port.isdigit():
                                self.write({'code': -1, 'msg': u'FastCGI服务器地址 %s 输入有误！' % fastcgi_pass})
                                return
                        location['fastcgi_pass'] = fastcgi_pass
                    elif loc['engine'] == 'redirect':
                        if not 'url' in locsetting or not locsetting['url']:
                            self.write({'code': -1, 'msg': u'请输入要跳转到的 URL 地址！'})
                            return
                        if not utils.is_url(locsetting['url']):
                            self.write({'code': -1, 'msg': u'跳转到的 URL 地址“%s”格式有误，请检查是否添加了 http:// 或 https:// 等！' % locsetting['url']})
                            return
                        location['redirect_url'] = locsetting['url']
                        if 'type' in locsetting and locsetting['type'] in ('301', '302'):
                            location['redirect_type'] = locsetting['type'] 
                        if 'option' in locsetting and locsetting['option'] in ('keep', 'ignore'):
                            location['redirect_option'] = locsetting['option'] 
                    elif loc['engine'] == 'proxy':
                        if not 'backends' in locsetting or not locsetting['backends']:
                            self.write({'code': -1, 'msg': u'反向代理后端不能为空！'})
                            return
                        if not 'protocol' in locsetting or not locsetting['protocol'] in ('http', 'https'):
                            self.write({'code': -1, 'msg': u'后端协议选择有误！'})
                            return
                        location['proxy_protocol'] = locsetting['protocol']
                        if 'host' in locsetting and utils.is_valid_domain(_u(locsetting['host'])):
                            location['proxy_host'] = locsetting['host']
                        if 'realip' in locsetting:
                            location['proxy_realip'] = locsetting['realip'] and True or False

                        backends = [backend for backend in locsetting['backends']
                            if 'server' in backend and backend['server'].strip()]
                        if 'charset' in locsetting:
                            if not locsetting['charset'] in charsets:
                                self.write({'code': -1, 'msg': u'请选择有效的字符编码！'})
                                return
                            if locsetting['charset']: location['proxy_charset'] = locsetting['charset']
                        if len(backends) == 0:
                            self.write({'code': -1, 'msg': u'反向代理后端不能为空！'})
                            return
                        elif len(backends) > 1:   # multi backends have load balance setting
                            if not 'balance' in locsetting or not locsetting['balance'] in ('weight', 'ip_hash', 'least_conn'):
                                self.write({'code': -1, 'msg': u'请设置负载均衡策略！'})
                                return
                            location['proxy_balance'] = locsetting['balance']
                            if 'keepalive' in locsetting:
                                if locsetting['keepalive'] and not locsetting['keepalive'].isdigit():
                                    self.write({'code': -1, 'msg': u'后端保持连接数必须是数字！'})
                                    return
                                if locsetting['keepalive']:
                                    location['proxy_keepalive'] = locsetting['keepalive']

                        location['proxy_backends'] = []
                        for backend in backends:
                            if not 'server' in backend:
                                self.write({'code': -1, 'msg': u'后端地址输入有误！'})
                                return
                            fields = backend['server'].split(':', 1)
                            if len(fields) > 1:
                                server, port = fields
                            else:
                                server = fields[0]
                                port = None
                            if not utils.is_valid_domain(_u(server)) or port and not port.isdigit():
                                self.write({'code': -1, 'msg': u'后端地址 %s 输入有误！' % backend['server']})
                                return
                            proxy_backend = {'server': backend['server']}
                            if len(backends) > 1:
                                if location['proxy_balance'] in ('weight', 'ip_hash'):
                                    if location['proxy_balance'] == 'weight':
                                        if 'weight' in backend:
                                            if backend['weight'] and not backend['weight'].isdigit():
                                                self.write({'code': -1, 'msg': u'后端权重值必须为数字！'})
                                                return
                                            if backend['weight']: proxy_backend['weight'] = backend['weight']
                                    if 'fail_timeout' in backend and 'max_fails' in backend:
                                        if backend['fail_timeout'] and not backend['fail_timeout'].isdigit():
                                            self.write({'code': -1, 'msg': u'后端失效检测超时必须为数字！'})
                                            return
                                        if backend['max_fails'] and not backend['max_fails'].isdigit():
                                            self.write({'code': -1, 'msg': u'后端失效检测次数必须为数字！'})
                                            return
                                        if backend['fail_timeout']: proxy_backend['fail_timeout'] = backend['fail_timeout']
                                        if backend['max_fails']: proxy_backend['max_fails'] = backend['max_fails']
                            location['proxy_backends'].append(proxy_backend)
                        
                        if 'proxy_cache_enable' in locsetting and locsetting['proxy_cache_enable']:
                            if not 'proxy_cache' in locsetting or locsetting['proxy_cache'] == '':
                                self.write({'code': -1, 'msg': u'请选择缓存区域！'})
                                return
                            location['proxy_cache'] = locsetting['proxy_cache']
                            if 'proxy_cache_min_uses' in locsetting and locsetting['proxy_cache_min_uses'] != '':
                                if not locsetting['proxy_cache_min_uses'].isdigit():
                                    self.write({'code': -1, 'msg': u'缓存条件的次数必须为数字！'})
                                    return
                                location['proxy_cache_min_uses'] = locsetting['proxy_cache_min_uses']
                            if 'proxy_cache_methods_post' in locsetting and locsetting['proxy_cache_methods_post']:
                                location['proxy_cache_methods'] = 'POST'
                            if 'proxy_cache_key' in locsetting:
                                t = []
                                ck = locsetting['proxy_cache_key']
                                if 'schema' in ck and ck['schema']:
                                    t.append('$scheme')
                                if 'host' in ck and ck['host']:
                                    t.append('$host')
                                if 'proxy_host' in ck and ck['proxy_host']:
                                    t.append('$proxy_host')
                                if 'uri' in ck and ck['uri']:
                                    t.append('$request_uri')
                                if len(t) > 0:
                                    location['proxy_cache_key'] = ''.join(t)
                            if 'proxy_cache_valid' in locsetting:
                                t = []
                                cvs = locsetting['proxy_cache_valid']
                                for cv in cvs:
                                    if not 'code' in cv or not 'time' in cv or not 'time_unit' in cv:
                                        continue
                                    if cv['code'] not in ('200', '301', '302', '404', '500', '502', '503', '504', 'any'):
                                        self.write({'code': -1, 'msg': u'缓存过期规则的状态码有误！'})
                                        return
                                    if not cv['time'].isdigit():
                                        self.write({'code': -1, 'msg': u'缓存过期规则的过期时间必须为数字！'})
                                        return
                                    if not cv['time_unit'] in ('s', 'm', 'h', 'd'):
                                        self.write({'code': -1, 'msg': u'缓存过期规则的过期时间单位有误！'})
                                        return
                                    t.append({'code': cv['code'], 'time': '%s%s' % (cv['time'], cv['time_unit'])})
                                if len(t)>0: location['proxy_cache_valid'] = t
                            if 'proxy_cache_use_stale' in locsetting:
                                t = []
                                cus = locsetting['proxy_cache_use_stale']
                                for k,v in cus.items():
                                    if not k in ('error', 'timeout', 'invalid_header', 'updating',
                                        'http_500', 'http_502', 'http_503', 'http_504', 'http_404') or not v: continue
                                    t.append(k)
                                if len(t)>0: location['proxy_cache_use_stale'] = t
                            if 'proxy_cache_lock' in locsetting and locsetting['proxy_cache_lock']:
                                location['proxy_cache_lock'] = True
                                if 'proxy_cache_lock_timeout' in locsetting:
                                    if not locsetting['proxy_cache_lock_timeout'].isdigit():
                                        self.write({'code': -1, 'msg': u'缓存锁定时间必须为数字！'})
                                        return
                                    location['proxy_cache_lock_timeout'] = locsetting['proxy_cache_lock_timeout']

                    elif loc['engine'] == 'error':
                        if not 'code' in locsetting or not locsetting['code']:
                            self.write({'code': -1, 'msg': u'请选择错误代码！'})
                            return
                        if locsetting['code'] not in ('401', '403', '404', '500', '502'):
                            self.write({'code': -1, 'msg': u'错误代码选择有误！'})
                            return
                        location['error_code'] = locsetting['code']
                    locations.append(location)

            #print(server_names)
            #print(listens)
            #print(charset)
            #print(index)
            #print(locations)
            #print(limit_rate)
            #print(limit_conn)
            #print(ssl_crt)
            #print(ssl_key)
            #print(rewrite_rules)

            if action == 'addserver':
                if not nginx.addserver(server_names, listens,
                    charset=charset, index=index, locations=locations,
                    limit_rate=limit_rate, limit_conn=limit_conn,
                    ssl_crt=ssl_crt, ssl_key=ssl_key,
                    rewrite_rules=rewrite_rules, version=version):
                    self.write({'code': -1, 'msg': u'新站点添加失败！请检查站点域名是否重复。'})
                else:
                    self.write({'code': 0, 'msg': u'新站点添加成功！'})
            else:
                if not nginx.updateserver(old_server_ip, old_server_port, old_server_name,
                    server_names, listens,
                    charset=charset, index=index, locations=locations,
                    limit_rate=limit_rate, limit_conn=limit_conn,
                    ssl_crt=ssl_crt, ssl_key=ssl_key,
                    rewrite_rules=rewrite_rules, version=version):
                    self.write({'code': -1, 'msg': u'站点设置更新失败！请检查配置信息（如域名是否重复？）'})
                else:
                    self.write({'code': 0, 'msg': u'站点设置更新成功！'})

    def mysql(self):
        action = self.get_argument('action', '')
        password = self.get_argument('password', '')

        if action == 'updatepwd':
            newpassword = self.get_argument('newpassword', '')
            newpasswordc = self.get_argument('newpasswordc', '')

            if newpassword != newpasswordc:
                self.write({'code': -1, 'msg': u'两次密码输入不一致！'})
                return

            if mysql.updatepwd(_u(newpassword), _u(password)):
                self.write({'code': 0, 'msg': u'密码设置成功！'})
            else:
                self.write({'code': -1, 'msg': u'密码设置失败！'})

        elif action == 'checkpwd':
            if mysql.checkpwd(_u(password)):
                self.write({'code': 0, 'msg': u'密码验证成功！'})
            else:
                self.write({'code': -1, 'msg': u'密码验证失败！（密码不正确，或 MySQL 服务未启动）'})

        elif action == 'alter_database':
            dbname = self.get_argument('dbname', '')
            collation = self.get_argument('collation', '')
            rt = mysql.alter_database(_u(password), _u(dbname), collation=_u(collation))
            if rt:
                self.write({'code': 0, 'msg': u'数据库编码保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'数据库编码保存失败！'})

    def php(self):
        action = self.get_argument('action', '')

        if action == 'getphpsettings':
            settings = php.loadconfig('php')
            self.write({'code': 0, 'msg': '', 'data': settings})

        elif action == 'getfpmsettings':
            settings = php.loadconfig('php-fpm')
            self.write({'code': 0, 'msg': '', 'data': settings})

        elif action == 'updatephpsettings':
            short_open_tag = self.get_argument('short_open_tag', '')
            expose_php = self.get_argument('expose_php', '')
            max_execution_time = self.get_argument('max_execution_time', '')
            memory_limit = self.get_argument('memory_limit', '')
            display_errors = self.get_argument('display_errors', '')
            post_max_size = self.get_argument('post_max_size', '')
            upload_max_filesize = self.get_argument('upload_max_filesize', '')
            date_timezone = self.get_argument('date.timezone', '')

            short_open_tag = short_open_tag.lower() == 'on' and 'On' or 'Off'
            expose_php = expose_php.lower() == 'on' and 'On' or 'Off'
            display_errors = display_errors.lower() == 'on' and 'On' or 'Off'

            if not max_execution_time == '' and not max_execution_time.isdigit():
                self.write({'code': -1, 'msg': u'max_execution_time 必须为数字！'})
                return
            if not memory_limit == '' and not memory_limit.isdigit():
                self.write({'code': -1, 'msg': u'memory_limit 必须为数字！'})
                return
            if not post_max_size == '' and not post_max_size.isdigit():
                self.write({'code': -1, 'msg': u'post_max_size 必须为数字！'})
                return
            if not upload_max_filesize == '' and not upload_max_filesize.isdigit():
                self.write({'code': -1, 'msg': u'upload_max_filesize 必须为数字！'})
                return

            memory_limit = '%sM' % memory_limit
            post_max_size = '%sM' % post_max_size
            upload_max_filesize = '%sM' % upload_max_filesize

            php.ini_set('short_open_tag', short_open_tag, initype='php')
            php.ini_set('expose_php', expose_php, initype='php')
            php.ini_set('max_execution_time', max_execution_time, initype='php')
            php.ini_set('memory_limit', memory_limit, initype='php')
            php.ini_set('display_errors', display_errors, initype='php')
            php.ini_set('post_max_size', post_max_size, initype='php')
            php.ini_set('upload_max_filesize', upload_max_filesize, initype='php')
            php.ini_set('date.timezone', date_timezone, initype='php')

            self.write({'code': 0, 'msg': u'PHP设置保存成功！'})

        elif action == 'updatefpmsettings':
            listen = self.get_argument('listen', '')
            pm = self.get_argument('pm', '')
            pm_max_children = self.get_argument('pm.max_children', '')
            pm_start_servers = self.get_argument('pm.start_servers', '')
            pm_min_spare_servers = self.get_argument('pm.min_spare_servers', '')
            pm_max_spare_servers = self.get_argument('pm.max_spare_servers', '')
            pm_max_requests = self.get_argument('pm.max_requests', '')
            request_terminate_timeout = self.get_argument('request_terminate_timeout', '')
            request_slowlog_timeout = self.get_argument('request_slowlog_timeout', '')

            pm = pm.lower() == 'on' and 'dynamic' or 'static'
            if not pm_max_children == '' and not pm_max_children.isdigit():
                self.write({'code': -1, 'msg': u'pm.max_children 必须为数字！'})
                return
            if not pm_start_servers == '' and not pm_start_servers.isdigit():
                self.write({'code': -1, 'msg': u'pm.start_servers 必须为数字！'})
                return
            if not pm_min_spare_servers == '' and not pm_min_spare_servers.isdigit():
                self.write({'code': -1, 'msg': u'pm.min_spare_servers 必须为数字！'})
                return
            if not pm_max_spare_servers == '' and not pm_max_spare_servers.isdigit():
                self.write({'code': -1, 'msg': u'pm.max_spare_servers 必须为数字！'})
                return
            if not pm_max_requests == '' and not pm_max_requests.isdigit():
                self.write({'code': -1, 'msg': u'pm.max_requests 必须为数字！'})
                return
            if not request_terminate_timeout == '' and not request_terminate_timeout.isdigit():
                self.write({'code': -1, 'msg': u'request_terminate_timeout 必须为数字！'})
                return
            if not request_slowlog_timeout == '' and not request_slowlog_timeout.isdigit():
                self.write({'code': -1, 'msg': u'request_slowlog_timeout 必须为数字！'})
                return

            php.ini_set('listen', listen, initype='php-fpm')
            php.ini_set('pm', pm, initype='php-fpm')
            php.ini_set('pm.max_children', pm_max_children, initype='php-fpm')
            php.ini_set('pm.start_servers', pm_start_servers, initype='php-fpm')
            php.ini_set('pm.min_spare_servers', pm_min_spare_servers, initype='php-fpm')
            php.ini_set('pm.max_spare_servers', pm_max_spare_servers, initype='php-fpm')
            php.ini_set('pm.max_requests', pm_max_requests, initype='php-fpm')
            php.ini_set('request_terminate_timeout', request_terminate_timeout, initype='php-fpm')
            php.ini_set('request_slowlog_timeout', request_slowlog_timeout, initype='php-fpm')
            
            self.write({'code': 0, 'msg': u'PHP FastCGI 设置保存成功！'})

    def ssh(self):
        action = self.get_argument('action', '')

        if action == 'getsettings':
            port = ssh.cfg_get('Port')
            enable_pwdauth = ssh.cfg_get('PasswordAuthentication') == 'yes'
            enable_pubkauth = ssh.cfg_get('PubkeyAuthentication') == 'yes'
            subsystem = ssh.cfg_get('Subsystem')
            enable_sftp = subsystem and 'sftp' in subsystem
            pubkey_path = '/root/.ssh/sshkey_inpanel.pub'
            prvkey_path = '/root/.ssh/sshkey_inpanel'
            self.write({'code': 0, 'msg': '获取 SSH 服务配置信息成功！', 'data': {
               'port': port,
               'enable_pwdauth': enable_pwdauth,
               'enable_pubkauth': enable_pubkauth,
               'enable_sftp': enable_sftp,
               'pubkey': isfile(pubkey_path) and pubkey_path or '',
               'prvkey': isfile(prvkey_path) and prvkey_path or '',
            }})

        elif action == 'savesettings':
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许修改 SSH 服务设置！'})
                return

            port = self.get_argument('port', '')
            if port: ssh.cfg_set('Port', port)
            enable_pwdauth = self.get_argument('enable_pwdauth', '')
            if enable_pwdauth: ssh.cfg_set('PasswordAuthentication', enable_pwdauth=='on' and 'yes' or 'no')
            enable_pubkauth = self.get_argument('enable_pubkauth', '')
            if enable_pubkauth:
                if enable_pubkauth == 'on':
                    pubkey_path = self.get_argument('pubkey', '')
                    if not isfile(pubkey_path):
                        self.write({'code': -1, 'msg': u'公钥文件不存在！'})
                        return
                ssh.cfg_set('PubkeyAuthentication', enable_pubkauth=='on' and 'yes' or 'no')
                ssh.cfg_set('AuthorizedKeysFile', pubkey_path)

            enable_sftp = self.get_argument('enable_sftp', '')
            if enable_sftp: ssh.cfg_set('Subsystem', 'sftp /usr/libexec/openssh/sftp-server', enable_sftp!='on')
            self.write({'code': 0, 'msg': 'SSH 服务配置保存成功！'})

    def cron(self):
        # cron jobs management
        action = self.get_argument('action', '')

        if action == 'get_settings':
            self.write({'code': 0, 'msg': u'获取 Cron 服务配置信息成功！', 'data': cron.load_config()})
        if action == 'save_settings':
            mailto = self.get_argument('mailto', '')
            rt = cron.update_config({'mailto': _u(mailto)})
            if rt:
                self.write({'code': 0, 'msg': u'设置保存成功！'})
            else:
                self.write({'code': -1, 'msg': u'设置保存失败！'})

        user = self.get_argument('user', '')
        level = self.get_argument('level', 'normal')
        if action == 'cron_list':
            self.write({'code': 0, 'msg': u'获取定时任务成功！','data': cron.cron_list(user=user, level=level)})
        elif action in ('cron_add', 'cron_mod'):
            command = self.get_argument('command', '')
            if command == '':
                self.write({'code': -1, 'msg': u'请输入命令！'})
                return
            if level == 'system' and user == '':
                self.write({'code': -1, 'msg': u'请输入用户'})
                return

            minute = self.get_argument('minute', '')
            hour = self.get_argument('hour', '')
            day = self.get_argument('day', '')
            month = self.get_argument('month', '')
            weekday = self.get_argument('weekday', '')
            weekday = self.get_argument('weekday', '')
            if action == 'cron_add':
                if cron.cron_add(user, minute, hour, day, month, weekday, command, level):
                    self.write({'code': 0, 'msg': u'定时任务添加成功！'})
                else:
                    self.write({'code': -1, 'msg': u'定时任务添加失败！'})
            elif action == 'cron_mod':
                cronid = self.get_argument('cronid', '')
                currlist = self.get_argument('currlist', '')
                if cron.cron_mod(user, cronid, minute, hour, day, month, weekday, command, level, currlist):
                    self.write({'code': 0, 'msg': u'定时任务修改成功！'})
                else:
                    self.write({'code': -1, 'msg': u'定时任务修改失败！'})
        elif action == 'cron_del':
            cronid = self.get_argument('cronid', '')
            currlist = self.get_argument('currlist', '')
            if cron.cron_del(user, cronid, level, currlist):
                self.write({'code': 0, 'msg': u'定时任务删除成功！'})
            else:
                self.write({'code': -1, 'msg': u'定时任务删除失败！'})

    def vsftpd(self):
        action = self.get_argument('action', '')
        if action == 'getsettings':
            self.write({'code': 0, 'msg': 'vsftpd 配置信息获取成功！', 'data': vsftpd.get_config()})
        elif action == 'savesettings':
            self.write({'code': 0, 'msg': 'vsftpd 服务配置保存成功！', 'data': vsftpd.set_config()})

    def named(self):
        named.web_response(self)

    def lighttpd(self):
        lighttpd.web_response(self)

    def proftpd(self):
        proftpd.web_response(self)

    def pureftpd(self):
        pureftpd.web_response(self)

    def shell(self):
        action = self.get_argument('action', '')
        cmd = self.get_argument('cmd', '')
        cwd = self.get_argument('cwd', '')
        if action == 'exec_command':
            self.write({'code': 0, 'msg': u'命令已发送', 'data': shell.exec_command(_u(cmd), _u(cwd))})

class PageHandler(RequestHandler):
    """Return single page.
    """
    def get(self, op, action):
        try:
            self.authed()
        except:
            self.write(u'<!DOCTYPE html><html><head><title>Permission Denied</title><meta charset="utf-8"/></head><body>没有权限，请<a href="/">登录</a>后再查看该页！<body></html>')
            return
        if hasattr(self, op):
            getattr(self, op)(action)
        else:
            self.write(u'未定义的操作！')

    def php(self, action):
        if action == 'phpinfo':
            # =PHPE9568F34-D428-11d2-A769-00AA001ACF42 (PHP Logo)
            # =PHPE9568F35-D428-11d2-A769-00AA001ACF42 (Zend logo)
            # =PHPB8B5F2A0-3C92-11d3-A3A9-4C7B08C10000 (PHP Credits)
            # redirect them to http://php.net/index.php?***
            if self.request.query.startswith('=PHP'):
                self.redirect('http://www.php.net/index.php?%s' % self.request.query)
            else:
                self.write(php.phpinfo())


class BackendHandler(RequestHandler):
    """Backend process manager
    """
    jobs = {}
    locks = {}

    def _lock_job(self, lockname):
        cls = BackendHandler
        if lockname in cls.locks:
            return False
        cls.locks[lockname] = True
        return True

    def _unlock_job(self, lockname):
        cls = BackendHandler
        if not lockname in cls.locks: return False
        del cls.locks[lockname]
        return True

    def _start_job(self, jobname):
        cls = BackendHandler
        # check if the job is running
        if jobname in cls.jobs and cls.jobs[jobname]['status'] == 'running':
            return False

        cls.jobs[jobname] = {'status': 'running', 'msg': ''}
        return True

    def _update_job(self, jobname, code, msg):
        cls = BackendHandler
        cls.jobs[jobname]['code'] = code
        cls.jobs[jobname]['msg'] = msg
        return True

    def _get_job(self, jobname):
        cls = BackendHandler
        if not jobname in cls.jobs:
            return {'status': 'none', 'code': -1, 'msg': ''}
        return cls.jobs[jobname]

    def _finish_job(self, jobname, code, msg, data=None):
        cls = BackendHandler
        cls.jobs[jobname]['status'] = 'finish'
        cls.jobs[jobname]['code'] = code
        cls.jobs[jobname]['msg'] = msg
        if data: cls.jobs[jobname]['data'] = data

    def get(self, jobname):
        """Get the status of the new process
        """
        self.authed()
        self.write(self._get_job(_u(jobname)))

    def _call(self, callback):
        #with tornado.stack_context.NullContext():
        tornado.ioloop.IOLoop.instance().add_callback(callback)

    def post(self, jobname):
        """Create a new backend process
        """
        self.authed()

        # centos/redhat only job
        if jobname in ('yum_repolist', 'yum_installrepo', 'yum_info',
                       'yum_install', 'yum_uninstall', 'yum_ext_info'):
            if self.settings['dist_name'] not in ('centos', 'redhat'):
                self.write({'code': -1, 'msg': u'不支持的系统类型！'})
                return

        if self.config.get('runtime', 'mode') == 'demo':
            if jobname in ('update', 'datetime', 'swapon', 'swapoff', 'mount', 'umount', 'format'):
                self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                return

        if jobname == 'update':
            self._call(self.update)
        elif jobname in ('service_restart', 'service_start', 'service_stop'):
            name = self.get_argument('name', '')
            service = self.get_argument('service', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if service in ('network', 'sshd', 'inpanel', 'iptables'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                    return

            if service not in Service.service_items:
                self.write({'code': -1, 'msg': u'未支持的服务！'})
                return
            if not name: name = service
            dummy, action = jobname.split('_')
            if service != '':
                self._call(partial(self.service,
                        _u(action),
                        _u(service),
                        _u(name)))
        elif jobname == 'datetime':
            newdatetime = self.get_argument('datetime', '')
            # check datetime format
            try:
                datetime.strptime(newdatetime, '%Y-%m-%d %H:%M:%S')
            except:
                self.write({'code': -1, 'msg': u'时间格式有错误！'})
                return
            self._call(partial(self.datetime,
                        _u(newdatetime)))
        elif jobname in ('swapon', 'swapoff'):
            devname = self.get_argument('devname', '')
            if jobname == 'swapon':
                action = 'on'
            else:
                action = 'off'
            self._call(partial(self.swapon,
                        _u(action),
                        _u(devname)))
        elif jobname in ('mount', 'umount'):
            devname = self.get_argument('devname', '')
            mountpoint = self.get_argument('mountpoint', '')
            fstype = self.get_argument('fstype', '')
            if jobname == 'mount':
                action = 'mount'
            else:
                action = 'umount'
            self._call(partial(self.mount,
                        _u(action),
                        _u(devname),
                        _u(mountpoint),
                        _u(fstype)))
        elif jobname == 'format':
            devname = self.get_argument('devname', '')
            fstype = self.get_argument('fstype', '')
            self._call(partial(self.format,
                        _u(devname),
                        _u(fstype)))
        elif jobname == 'yum_repolist':
            self._call(self.yum_repolist)
        elif jobname == 'yum_installrepo':
            repo = self.get_argument('repo', '')
            self._call(partial(self.yum_installrepo,
                        _u(repo)))
        elif jobname == 'yum_info':
            pkg = self.get_argument('pkg', '')
            repo = self.get_argument('repo', '*')
            option = self.get_argument('option', '')
            if option == 'update':
                if not pkg in [v for k,vv in yum.yum_pkg_alias.items() for v in vv]:
                    self.write({'code': -1, 'msg': u'未支持的软件包！'})
                    return
            else:
                option = 'install'
                if not pkg in yum.yum_pkg_alias:
                    self.write({'code': -1, 'msg': u'未支持的软件包！'})
                    return
                if repo not in yum.yum_repolist + ('installed', '*'):
                    self.write({'code': -1, 'msg': u'未知的软件源 %s！' % repo})
                    return
            self._call(partial(self.yum_info,
                        _u(pkg),
                        _u(repo),
                        _u(option)))
        elif jobname in ('yum_install', 'yum_uninstall', 'yum_update'):
            repo = self.get_argument('repo', '')
            pkg = self.get_argument('pkg', '')
            ext = self.get_argument('ext', '')
            version = self.get_argument('version', '')
            release = self.get_argument('release', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if pkg in ('sshd', 'iptables'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                    return

            if not pkg in yum.yum_pkg_relatives:
                self.write({'code': -1, 'msg': u'软件包不存在！'})
                return
            if ext and not ext in yum.yum_pkg_relatives[pkg]:
                self.write({'code': -1, 'msg': u'扩展不存在！'})
                return
            if jobname == 'yum_install':
                if repo not in yum.yum_repolist:
                    self.write({'code': -1, 'msg': u'未知的软件源 %s！' % repo})
                    return
                handler = self.yum_install
            elif jobname == 'yum_uninstall':
                handler = self.yum_uninstall
            elif jobname == 'yum_update':
                handler = self.yum_update
            self._call(partial(handler,
                        _u(repo),
                        _u(pkg),
                        _u(version),
                        _u(release),
                        _u(ext)))
        elif jobname == 'yum_ext_info':
            pkg = self.get_argument('pkg', '')
            if not pkg in yum.yum_pkg_relatives:
                self.write({'code': -1, 'msg': u'软件包不存在！'})
                return
            self._call(partial(self.yum_ext_info,
                        _u(pkg)))
        elif jobname in ('move', 'copy'):
            srcpath = self.get_argument('srcpath', '')
            despath = self.get_argument('despath', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if jobname == 'move':
                    if not srcpath.startswith('/var/www') or not despath.startswith('/var/www'):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                        return
                elif jobname == 'copy':
                    if not despath.startswith('/var/www'):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许修改除 /var/www 以外的目录！'})
                        return

            if not exists(srcpath):
                if not exists(srcpath.strip('*')):
                    self.write({'code': -1, 'msg': u'源路径不存在！'})
                    return
            if jobname == 'copy':
                handler = self.copy
            elif jobname == 'move':
                handler = self.move
            self._call(partial(handler,
                        _u(srcpath),
                        _u(despath)))
        elif jobname == 'remove':
            paths = self.get_argument('paths', '')
            paths = _u(paths).split(',')

            if self.config.get('runtime', 'mode') == 'demo':
                for p in paths:
                    if not p.startswith('/var/www') and not p.startswith(self.settings['package_path']):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下执行删除操作！'})
                        return

            self._call(partial(self.remove, paths))
        elif jobname == 'compress':
            zippath = self.get_argument('zippath', '')
            paths = self.get_argument('paths', '')
            paths = _u(paths).split(',')

            if self.config.get('runtime', 'mode') == 'demo':
                if not zippath.startswith('/var/www'):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下创建压缩包！'})
                    return
                for p in paths:
                    if not p.startswith('/var/www'):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下创建压缩包！'})
                        return

            self._call(partial(self.compress,
                        _u(zippath), paths))
        elif jobname == 'decompress':
            zippath = self.get_argument('zippath', '')
            despath = self.get_argument('despath', '')

            if self.config.get('runtime', 'mode') == 'demo':
                if not zippath.startswith('/var/www') and not zippath.startswith(self.settings['package_path']) or \
                   not despath.startswith('/var/www') and not despath.startswith(self.settings['package_path']):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下执行解压操作！'})
                    return

            self._call(partial(self.decompress,
                        _u(zippath),
                        _u(despath)))
        elif jobname == 'ntpdate':
            server = self.get_argument('server', '')
            self._call(partial(self.ntpdate, _u(server)))
        elif jobname == 'chown':
            paths = _u(self.get_argument('paths', ''))
            paths = paths.split(',')

            if self.config.get('runtime', 'mode') == 'demo':
                for p in paths:
                    if not p.startswith('/var/www'):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下执行此操作！'})
                        return

            a_user = _u(self.get_argument('user', ''))
            a_group = _u(self.get_argument('group', ''))
            recursively = self.get_argument('recursively', '')
            option = recursively == 'on' and '-R' or ''
            self._call(partial(self.chown, paths, a_user, a_group, option))
        elif jobname == 'chmod':
            paths = _u(self.get_argument('paths', ''))
            paths = paths.split(',')

            if self.config.get('runtime', 'mode') == 'demo':
                for p in paths:
                    if not p.startswith('/var/www'):
                        self.write({'code': -1, 'msg': u'DEMO状态不允许在 /var/www 以外的目录下执行此操作！'})
                        return

            perms = _u(self.get_argument('perms', ''))
            recursively = self.get_argument('recursively', '')
            option = recursively == 'on' and '-R' or ''
            self._call(partial(self.chmod, paths, perms, option))
        elif jobname == 'wget':
            url = _u(self.get_argument('url', ''))
            path = _u(self.get_argument('path', ''))

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www') and not path.startswith(self.settings['package_path']):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许下载到 /var/www 以外的目录！'})
                    return

            self._call(partial(self.wget, url, path))
        elif jobname == 'mysql_fupdatepwd':
            password = _u(self.get_argument('password', ''))
            passwordc = _u(self.get_argument('passwordc', ''))
            if password != passwordc:
                self.write({'code': -1, 'msg': u'两次密码输入不一致！'})
                return
            self._call(partial(self.mysql_fupdatepwd, password))
        elif jobname == 'mysql_databases':
            password = _u(self.get_argument('password', ''))
            self._call(partial(self.mysql_databases, password))
        elif jobname == 'mysql_dbinfo':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            self._call(partial(self.mysql_dbinfo, password, dbname))
        elif jobname == 'mysql_users':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            self._call(partial(self.mysql_users, password, dbname))
        elif jobname == 'mysql_rename':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            newname = _u(self.get_argument('newname', ''))
            if dbname == newname:
                self.write({'code': -1, 'msg': u'数据库名无变化！'})
                return
            self._call(partial(self.mysql_rename, password, dbname, newname))
        elif jobname == 'mysql_create':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            collation = _u(self.get_argument('collation', ''))
            self._call(partial(self.mysql_create, password, dbname, collation))
        elif jobname == 'mysql_export':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            path = _u(self.get_argument('path', ''))

            if not path:
                self.write({'code': -1, 'msg': u'请选择数据库导出目录！'})
                return

            if self.config.get('runtime', 'mode') == 'demo':
                if not path.startswith('/var/www') and not path.startswith(self.settings['package_path']):
                    self.write({'code': -1, 'msg': u'DEMO状态不允许导出到 /var/www 以外的目录！'})
                    return

            self._call(partial(self.mysql_export, password, dbname, path))
        elif jobname == 'mysql_drop':
            password = _u(self.get_argument('password', ''))
            dbname = _u(self.get_argument('dbname', ''))
            self._call(partial(self.mysql_drop, password, dbname))
        elif jobname == 'mysql_createuser':
            password = _u(self.get_argument('password', ''))
            user = _u(self.get_argument('user', ''))
            host = _u(self.get_argument('host', ''))
            pwd = _u(self.get_argument('pwd', ''))
            self._call(partial(self.mysql_createuser, password, user, host, pwd))
        elif jobname == 'mysql_userprivs':
            password = _u(self.get_argument('password', ''))
            username = _u(self.get_argument('username', ''))
            if not '@' in username:
                self.write({'code': -1, 'msg': u'用户不存在！'})
                return
            user, host = username.split('@', 1)
            self._call(partial(self.mysql_userprivs, password, user, host))
        elif jobname == 'mysql_updateuserprivs':
            password = _u(self.get_argument('password', ''))
            username = _u(self.get_argument('username', ''))
            privs = self.get_argument('privs', '')
            try:
                privs = tornado.escape.json_decode(privs)
            except:
                self.write({'code': -1, 'msg': u'权限数据有误！'})
                return
            dbname = _u(self.get_argument('dbname', ''))
            if not '@' in username:
                self.write({'code': -1, 'msg': u'用户不存在！'})
                return
            user, host = username.split('@', 1)
            privs = [
                priv.replace('_priv', '').replace('_', ' ').upper()
                    .replace('CREATE TMP TABLE', 'CREATE TEMPORARY TABLES')
                    .replace('SHOW DB', 'SHOW DATABASES')
                    .replace('REPL CLIENT', 'REPLICATION CLIENT')
                    .replace('REPL SLAVE', 'REPLICATION SLAVE')
                for priv, value in privs.items() if '_priv' in priv and value == 'Y']
            self._call(partial(self.mysql_updateuserprivs, password, user, host, privs, dbname))
        elif jobname == 'mysql_setuserpassword':
            password = _u(self.get_argument('password', ''))
            username = _u(self.get_argument('username', ''))
            if not '@' in username:
                self.write({'code': -1, 'msg': u'用户不存在！'})
                return
            user, host = username.split('@', 1)
            pwd = _u(self.get_argument('pwd', ''))
            self._call(partial(self.mysql_setuserpassword, password, user, host, pwd))
        elif jobname == 'mysql_dropuser':
            password = _u(self.get_argument('password', ''))
            username = _u(self.get_argument('username', ''))
            if not '@' in username:
                self.write({'code': -1, 'msg': u'用户不存在！'})
                return
            user, host = username.split('@', 1)
            user, host = user.strip(), host.strip()
            if user == 'root' and host != '%':
                self.write({'code': -1, 'msg': u'该用户不允许删除！'})
                return
            self._call(partial(self.mysql_dropuser, password, user, host))
        elif jobname == 'ssh_genkey':
            path = _u(self.get_argument('path', ''))
            password = _u(self.get_argument('password', ''))
            if not path: path = '/root/.ssh/sshkey_inpanel'
            self._call(partial(self.ssh_genkey, path, password))
        elif jobname == 'ssh_chpasswd':
            path = _u(self.get_argument('path', ''))
            oldpassword = _u(self.get_argument('oldpassword', ''))
            newpassword = _u(self.get_argument('newpassword', ''))
            if not path: path = '/root/.ssh/sshkey_inpanel'
            self._call(partial(self.ssh_chpasswd, path, oldpassword, newpassword))
        elif jobname in ('inpanel_install', 'inpanel_uninstall', 'inpanel_config'):
            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                return
            ssh_ip = self.get_argument('ssh_ip', '')
            ssh_port = self.get_argument('ssh_port', '22')
            ssh_user = self.get_argument('ssh_user', '')
            ssh_password = self.get_argument('ssh_password', '')
            instance_name = self.get_argument('instance_name', '')
            if jobname == 'inpanel_install':
                accessnet = self.get_argument('accessnet', 'public')
                accesskey = utils.gen_accesskey()
                accessport = '8888'
            elif jobname == 'inpanel_config':
                if not self.config.has_option('inpanel', instance_name):
                    self.write({'code': -1, 'msg': u'该服务器还未配置远程控制！'})
                    return
                accessdata = self.config.get('inpanel', instance_name)
                accessdata = accessdata.split('|')
                accesskey = accessdata[0]
            if jobname == 'inpanel_install':
                self._call(partial(self.inpanel_install,
                        _u(ssh_ip), _u(ssh_port), _u(ssh_user), _u(ssh_password),
                        _u(instance_name), _u(accessnet), _u(accessport), _u(accesskey)))
            elif jobname == 'inpanel_uninstall':
                self._call(partial(self.inpanel_uninstall,
                        _u(ssh_ip), _u(ssh_port), _u(ssh_user), _u(ssh_password),
                        _u(instance_name)))
            elif jobname == 'inpanel_config':
                self._call(partial(self.inpanel_config,
                        _u(ssh_ip), _u(ssh_port), _u(ssh_user), _u(ssh_password),
                        _u(accesskey)))
        elif jobname == 'uploadtoftp':
            address = self.get_argument('address', '')
            account = self.get_argument('account', '')
            password = self.get_argument('password', '')
            source = self.get_argument('source', '')
            target = self.get_argument('target', '')
            self._call(partial(self.uploadtoftp, _u(address), _u(account), _u(password), _u(source), _u(target)))
        else:   # undefined job
            self.write({'code': -1, 'msg': u'未定义的操作！'})
            return

        self.write({'code': 0, 'msg': ''})

    @tornado.gen.engine
    def update(self):
        if not self._start_job('update'): return

        root_path = self.settings['root_path']
        data_path = self.settings['data_path']
        distname = self.settings['dist_name']

        # don't do it in dev environment
        if exists('%s/../.git' % root_path):
            self._finish_job('update', 0, u'升级成功！')
            return

        # install the latest version
        http = tornado.httpclient.AsyncHTTPClient()
        response = yield tornado.gen.Task(http.fetch, core_api['latest'])
        if response.error:
            self._update_job('update', -1, u'获取版本信息失败！')
            return
        versioninfo = tornado.escape.json_decode(response.body)
        downloadurl = versioninfo['download']
        initscript = u'%s/scripts/%s/inpanel' % (root_path, distname)
        steps = [
            {
                'desc': u'正在备份当前配置文件...',
                'cmd': u'/bin/cp -f %s/config.ini /tmp/inpanel_config.ini' % data_path,
            }, {
                'desc': u'正在下载安装包...',
                'cmd': u'wget -q "%s" -O %s/inpanel.tar.gz' % (downloadurl, data_path),
            }, {
                'desc': u'正在创建解压目录...',
                'cmd': u'mkdir -p %s/inpanel' % data_path,
            }, {
                'desc': u'正在解压安装包...',
                'cmd': u'tar zxmf %s/inpanel.tar.gz -C %s/inpanel --strip-components 1' % (data_path, data_path),
            }, {
                'desc': u'正在删除旧版本...',
                'cmd': u'find %s -mindepth 1 -maxdepth 1 -path %s -prune -o -exec rm -rf {} \;' % (root_path, data_path),
            }, {
                'desc': u'正在复制新版本...',
                'cmd': u'find %s/inpanel -mindepth 1 -maxdepth 1 -exec cp -r {} %s \;' % (data_path, root_path),
            }, {
                'desc': u'正在删除旧的服务脚本...',
                'cmd': u'rm -f /etc/init.d/inpanel',
            }, {
                'desc': u'正在安装新的服务脚本...',
                'cmd': u'cp %s /etc/init.d/inpanel' % initscript
            }, {
                'desc': u'正在更改脚本权限...',
                'cmd': u'chmod +x /etc/init.d/inpanel %s/config.py %s/server.py' % (root_path, root_path),
            }, {
                'desc': u'正在删除安装临时文件...',
                'cmd': u'rm -rf %s/inpanel %s/inpanel.tar.gz' % (data_path, data_path)
            }, {
                'desc': u'正在恢复旧的配置文件...',
                'cmd': u'/bin/cp -f /tmp/inpanel_config.ini %s/config.ini' % data_path
            }, {
                'desc': u'正在删除旧的配置文件...',
                'cmd': u'rm -f /tmp/inpanel_config.ini'
            }
        ]
        for step in steps:
            desc = _u(step['desc'])
            cmd = _u(step['cmd'])
            self._update_job('update', 2, desc)
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result != 0:
                self._update_job('update', -1, desc+'失败！')
                break

        if result == 0:
            code = 0
            msg = u'升级成功！请刷新页面重新登录。'
        else:
            code = -1
            msg = u'升级失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job('update', code, msg)

    @tornado.gen.engine
    def service(self, action, service, name):
        """Service operation."""
        jobname = 'service_%s_%s' % (action, service)
        if not self._start_job(jobname): return

        action_str = {'start': u'启动', 'stop': u'停止', 'restart': u'重启'}
        self._update_job(jobname, 2, u'正在%s %s 服务...' % (action_str[action], _d(name)))

        # patch before start sendmail in redhat/centos 5.x
        # REF: http://www.mombu.com/gnu_linux/red-hat/t-why-does-sendmail-hang-during-rh-9-start-up-1068528.html
        if action == 'start' and service in ('sendmail', )\
            and self.settings['dist_name'] in ('redhat', 'centos')\
            and self.settings['dist_verint'] == 5:
            # check if current hostname line in /etc/hosts have a char '.'
            hostname = ServerInfo.hostname()
            hostname_found = False
            dot_found = False
            lines = []
            with open('/etc/hosts') as f:
                for line in f:
                    if not line.startswith('#') and not hostname_found:
                        fields = line.strip().split()
                        if hostname in fields:
                            hostname_found = True
                            # find '.' in this line
                            dot_found = any(field for field in fields[1:] if '.' in field)
                            if not dot_found:
                                line = '%s %s.localdomain\n' % (line.strip(), hostname)
                    lines.append(line)
            if not dot_found:
                with open('/etc/hosts', 'w') as f: f.writelines(lines)
        if self.settings['dist_verint'] < 7:
            cmd = '/etc/init.d/%s %s' % (service, action)
        else:
            cmd = '/bin/systemctl %s %s.service' % (action, service)
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'%s 服务%s成功！' % (_d(name), action_str[action])
        else:
            code = -1
            msg = u'%s 服务%s失败！<p style="margin:10px">%s</p>' % (_d(name), action_str[action], _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def datetime(self, newdatetime):
        """Set datetime using system's date command.
        """
        jobname = 'datetime'
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在设置系统时间...')

        cmd = 'date -s %s' % (quote(newdatetime), )
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'系统时间设置成功！'
        else:
            code = -1
            msg = u'系统时间设置失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def ntpdate(self, server):
        """Run ntpdate command to sync time.
        """
        jobname = 'ntpdate_%s' % server
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在从 %s 同步时间...' % server)
        cmd = 'ntpdate -u %s' % server
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            offset = output.split(' offset ')[-1].split()[0]
            msg = u'同步时间成功！（时间偏差 %s 秒）' % str(offset)
        else:
            code = -1
            if 'no server suitable' in output: # no server suitable for synchronization found
                msg = u'同步时间失败！没有找到合适同步服务器。'
            elif 'no servers can be used' in output: # no address associated with hostname
                msg = u'同步时间失败！没有找到同步服务器的地址'
            else:
                msg = u'同步时间失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def swapon(self, action, devname):
        """swapon or swapoff swap partition.
        """
        jobname = 'swapon_%s_%s' % (action, devname)
        if not self._start_job(jobname): return

        action_str = {'on': u'启用', 'off': u'停用'}
        self._update_job(jobname, 2, u'正在%s %s...' % \
                    (action_str[action], _d(devname)))

        if action == 'on':
            cmd = 'swapon /dev/%s' % devname
        else:
            cmd = 'swapoff /dev/%s' % devname

        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'%s %s 成功！' % (action_str[action], _d(devname))
        else:
            code = -1
            msg = u'%s %s 失败！<p style="margin:10px">%s</p>' % (action_str[action], _d(devname), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mount(self, action, devname, mountpoint, fstype):
        """Mount or umount using system's mount command.
        """
        jobname = 'mount_%s_%s' % (action, devname)
        if not self._start_job(jobname): return

        action_str = {'mount': u'挂载', 'umount': u'卸载'}
        self._update_job(jobname, 2, u'正在%s %s 到 %s...' % \
                    (action_str[action], _d(devname), _d(mountpoint)))

        if action == 'mount':
            # write config to /etc/fstab
            ServerSet.fstab(_u(devname), {
                'devname': _u(devname),
                'mount': _u(mountpoint),
                'fstype': _u(fstype),
            })
            cmd = 'mount -t %s /dev/%s %s' % (fstype, devname, mountpoint)
        else:
            cmd = 'umount /dev/%s' % (devname)

        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'%s %s 成功！' % (action_str[action], _d(devname))
        else:
            code = -1
            msg = u'%s %s 失败！<p style="margin:10px">%s</p>' % (action_str[action], _d(devname), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def format(self, devname, fstype):
        """Format partition using system's mkfs.* commands.
        """
        jobname = 'format_%s' % devname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在格式化 %s，可能需要较长时间，请耐心等候...' % _d(devname))

        if fstype in ('ext2', 'ext3', 'ext4'):
            cmd = 'mkfs.%s -F /dev/%s' % (fstype, devname)
        elif fstype in ('xfs', 'reiserfs', 'btrfs'):
            cmd = 'mkfs.%s -f /dev/%s' % (fstype, devname)
        elif fstype == 'swap':
            cmd = 'mkswap -f /dev/%s' % devname
        else:
            cmd = 'mkfs.%s /dev/%s' % (fstype, devname)
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'%s 格式化成功！' % _d(devname)
        else:
            code = -1
            msg = u'%s 格式化失败！<p style="margin:10px">%s</p>' % (_d(devname), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def yum_repolist(self):
        """Get yum repository list.
        """
        jobname = 'yum_repolist'
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，读取软件源列表失败。')
            return

        self._update_job(jobname, 2, u'正在获取软件源列表...')

        cmd = 'yum repolist --disableplugin=fastestmirror'
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        data = []
        if result == 0:
            code = 0
            msg = u'获取软件源列表成功！'
            lines = output.split('\n')
            for line in lines:
                if not line: continue
                repo = line.split()[0]
                if repo in yum.yum_repolist:
                    data.append(repo)
        else:
            code = -1
            msg = u'获取软件源列表失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg, data)
        self._unlock_job('yum')

    @tornado.gen.engine
    def yum_installrepo(self, repo):
        """Install yum repository.

        REFs:
        http://jyrxs.blogspot.com/2008/02/using-centos-5-repos-in-rhel5-server.html
        http://www.tuxradar.com/answers/440
        """
        jobname = 'yum_installrepo_%s' % repo
        if not self._start_job(jobname): return

        if repo not in yum.yum_repolist:
            self._finish_job(jobname, -1, u'不可识别的软件源！')
            self._unlock_job('yum')
            return

        self._update_job(jobname, 2, u'正在安装软件源 %s...' % _d(repo))

        arch = self.settings['arch']
        dist_verint = self.settings['dist_verint']

        cmds = []
        if repo == 'base':
            if dist_verint == 5:
                if self.settings['dist_name'] == 'redhat':
                    # backup system version info
                    cmds.append('cp -f /etc/redhat-release /etc/redhat-release.inpanel')
                    cmds.append('cp -f /etc/issue /etc/issue.inpanel')
                    #cmds.append('rpm -e redhat-release-notes-5Server --nodeps')
                    cmds.append('rpm -e redhat-release-5Server --nodeps')

            for rpm in yum.yum_reporpms[repo][dist_verint][arch]:
                cmds.append('rpm -U %s' % rpm)

            if exists('/etc/issue.inpanel'):
                cmds.append('cp -f /etc/issue.inpanel /etc/issue')
            if exists('/etc/redhat-release.inpanel'):
                cmds.append('cp -f /etc/redhat-release.inpanel /etc/redhat-release')

        elif repo in ('epel', 'CentALT'):
            # elif repo in ('epel', 'CentALT', 'ius'):
            # CentALT and ius depends on epel
            for rpm in yum.yum_reporpms['epel'][dist_verint][arch]:
                cmds.append('rpm -U %s' % rpm)

            # if dist_verint < 7:
            #     if repo in ('CentALT', 'ius'):
            #         for rpm in yum.yum_reporpms[repo][dist_verint][arch]:
            #             cmds.append('rpm -U %s' % rpm)

        elif repo == 'ius':
            # REF: https://ius.io/GettingStarted/#install-via-automation
            result, output = yield tornado.gen.Task(call_subprocess, self, yum.yum_repoinstallcmds['ius'], shell=True)
            if result != 0: error = True

        elif repo == '10gen':
            # REF: http://docs.mongodb.org/manual/tutorial/install-mongodb-on-redhat-centos-or-fedora-linux/
            with open('/etc/yum.repos.d/10gen.repo', 'w') as f:
                f.write(yum.yum_repostr['10gen'][self.settings['arch']])

        elif repo == 'mariadb':
            # MariaDB Repositories REF: http://downloads.mariadb.org/mariadb/repositories
            with open('/etc/yum.repos.d/mariadb.repo', 'w') as f:
                f.write(yum.yum_repostr['mariadb'][self.settings['arch']])

        elif repo == 'atomic':
            # REF: http://www.atomicorp.com/channels/atomic/
            result, output = yield tornado.gen.Task(call_subprocess, self, yum.yum_repoinstallcmds['atomic'], shell=True)
            if result != 0: error = True

        error = False
        for cmd in cmds:
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result !=0 and not 'already installed' in output:
                error = True
                break

        # CentALT doesn't have any mirror, we have make a mirror for it
        if repo == 'CentALT':
            repofile = '/etc/yum.repos.d/centalt.repo'
            if exists(repofile):
                lines = []
                baseurl_found = False
                with open(repofile) as f:
                    for line in f:
                        if line.startswith('baseurl='):
                            baseurl_found = True
                            line = '#%s' % line
                            lines.append(line)
                            # # add a mirrorlist line
                            # metalink = 'https://inpanel.org/mirrorlist?'\
                            #     'repo=centalt-%s&arch=$basearch' % self.settings['dist_verint']
                            # line = 'mirrorlist=%s\n' % metalink
                        lines.append(line)
                if baseurl_found:
                    with open(repofile, 'w') as f: f.writelines(lines)

        if not error:
            code = 0
            msg = u'软件源 %s 安装成功！' % _d(repo)
        else:
            code = -1
            msg = u'软件源 %s 安装失败！<p style="margin:10px">%s</p>' % (_d(repo), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def yum_info(self, pkg, repo, option):
        """Get package info in repository.

        Option can be 'install' or 'update'.
        """
        jobname = 'yum_info_%s' % pkg
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，读取软件包信息失败。')
            return

        self._update_job(jobname, 2, u'正在获取软件版本信息...')

        if repo == '*': repo = ''
        arch = self.settings['arch']
        if pkg in yum.yum_pkg_noarchitecture:
            arch = 'noarch'
        if option == 'install':
            cmds = ['yum info %s %s.%s --showduplicates --disableplugin=fastestmirror'
                    % (repo, alias, arch) for alias in yum.yum_pkg_alias[pkg]]
        else:
            cmds = ['yum info %s.%s --disableplugin=fastestmirror' % (pkg, arch)]

        data = []
        matched = False
        for cmd in cmds:
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result == 0:
                matched = True
                lines = output.split('\n')
                for line in lines:
                    if any(line.startswith(word)
                        for word in ('Name', 'Version', 'Release', 'Size',
                                     'Repo', 'From repo', 'Summary', 'URL', 'License')):
                        fields = line.strip().split(':', 1)
                        if len(fields) != 2: continue
                        field_name = fields[0].strip().lower().replace(' ', '_')
                        field_value = fields[1].strip()
                        if field_name == 'name':
                            data.append({})
                        if field_name == 'repo':
                            data[-1][field_name] = field_value.split('/')[0] # compatible to repo: "base/7/x86_64"
                        else:
                            data[-1][field_name] = field_value

        if matched:
            code = 0
            msg = u'获取软件版本信息成功！'
            data = [pkg for pkg in data if pkg['repo'] in yum.yum_repolist+('installed',)]
            if option == 'update' and len(data) == 1:
                msg = u'没有找到可用的新版本！'
        else:
            code = -1
            msg = u'获取软件版本信息失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg, data)
        self._unlock_job('yum')

    @tornado.gen.engine
    def yum_install(self, repo, pkg, version, release, ext):
        """Install specified version of package.
        """
        jobname = 'yum_install_%s_%s_%s_%s_%s' % (repo, pkg, ext, version, release)
        jobname = jobname.strip('_').replace('__', '_')
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，安装失败。')
            return

        if ext:
            self._update_job(jobname, 2, u'正在下载并安装扩展包，请耐心等候...')
        else:
            self._update_job(jobname, 2, u'正在下载并安装软件包，请耐心等候...')

        arch = self.settings['arch']
        if pkg in yum.yum_pkg_noarchitecture:
            arch = 'noarch'

        if ext: # install extension
            if version: 
                if release:
                    pkgs = ['%s-%s-%s.%s' % (ext, version, release, arch)]
                else:
                    pkgs = ['%s-%s.%s' % (ext, version, arch)]
            else:
                pkgs = ['%s.%s' % (ext, arch)]
        else:   # install package
            if version: # install special version
                if release:
                    pkgs = ['%s-%s-%s.%s' % (p, version, release, arch)
                        for p, pinfo in yum.yum_pkg_relatives[pkg].items() if pinfo['default']]
                else:
                    pkgs = ['%s-%s.%s' % (p, version, arch)
                        for p, pinfo in yum.yum_pkg_relatives[pkg].items() if pinfo['default']]
            else:   # or judge by the system
                pkgs = ['%s.%s' % (p, arch)
                    for p, pinfo in yum.yum_pkg_relatives[pkg].items() if pinfo['default']]
        repos = [repo, ]
        if repo in ('CentALT', 'ius', 'atomic', '10gen', 'mariadb'):
            repos.extend(['base', 'updates', 'epel'])
        exclude_repos = [r for r in yum.yum_repolist if r not in repos]

        endinstall = False
        hasconflict = False
        conflicts_backups = []
        while not endinstall:
            cmd = 'yum install -y %s --disablerepo=%s' % (' '.join(pkgs), ','.join(exclude_repos))
            #cmd = 'yum install -y %s' % (' '.join(pkgs), )
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            pkg_ext = ext and ext or pkg

            pkgstr = version and '%s v%s-%s' % (pkg_ext, version, release) or (pkg_ext)
            if result == 0:
                if hasconflict:
                    # install the conflict packages we just remove
                    cmd = 'yum install -y %s' % (' '.join(conflicts_backups), )
                    result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
                endinstall = True
                code = 0
                msg = u'%s 安装成功！' % _d(pkgstr)
            else:
                # check if conflicts occur
                # error message like this:
                #   Error: mysql55 conflicts with mysql
                # or:
                #   file /etc/my.cnf conflicts between attempted installs of mysql-libs-5.5.28-1.el6.x86_64 and mysql55-libs-5.5.28-2.ius.el6.x86_64
                #   file /usr/lib64/mysql/libmysqlclient.so.18.0.0 conflicts between attempted installs of mysql-libs-5.5.28-1.el6.x86_64 and mysql55-libs-5.5.28-2.ius.el6.x86_64
                clines = output.split('\n')
                for cline in clines:
                    if cline.startswith('Error:') and ' conflicts with ' in cline:
                        hasconflict = True
                        conflict_pkg = cline.split(' conflicts with ', 1)[1]
                        # remove the conflict package and packages depend on it
                        self._update_job(jobname, 2, u'检测到软件冲突，正在卸载处理冲突...')
                        tcmd = 'yum erase -y %s' % conflict_pkg
                        result, output = yield tornado.gen.Task(call_subprocess, self, tcmd)
                        if result == 0:
                            lines = output.split('\n')
                            conflicts_backups = []
                            linestart = False
                            for line in lines:
                                if not linestart:
                                    if not line.startswith('Removing for dependencies:'): continue
                                    linestart = True
                                if not line.strip(): break # end
                                fields = line.split()
                                conflicts_backups.append('%s-%s' % (fields[0], fields[2]))
                        else:
                            endinstall = True
                        break
                    elif 'conflicts between' in cline:
                        pass
                if not hasconflict:
                    endinstall = True
                if endinstall:
                    code = -1
                    msg = u'%s 安装失败！<p style="margin:10px">%s</p>' % (_d(pkgstr), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)
        self._unlock_job('yum')

    @tornado.gen.engine
    def yum_uninstall(self, repo, pkg, version, release, ext):
        """Uninstall specified version of package.
        """
        jobname = 'yum_uninstall_%s_%s_%s_%s' % (pkg, ext, version, release)
        jobname = jobname.strip('_').replace('__', '_')
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，卸载失败。')
            return

        if ext:
            self._update_job(jobname, 2, u'正在卸载扩展包...')
        else:
            self._update_job(jobname, 2, u'正在卸载软件包...')

        arch = self.settings['arch']
        if pkg in yum.yum_pkg_noarchitecture:
            arch = 'noarch'

        if ext:
            pkgs = ['%s-%s-%s.%s' % (ext, version, release, arch)]
        else:
            pkgs = ['%s-%s-%s.%s' % (p, version, release, arch)
                for p, pinfo in yum.yum_pkg_relatives[pkg].items()
                if 'base' in pinfo and pinfo['base']]
        ## also remove depends pkgs
        #for p, pinfo in yum.yum_pkg_relatives[pkg].items():
        #    if 'depends' in pinfo:
        #        pkgs += pinfo['depends']
        cmd = 'yum erase -y %s' % (' '.join(pkgs), )
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        pkg_ext = ext and ext or pkg
        if result == 0:
            code = 0
            msg = u'%s v%s-%s 卸载成功！' % (_d(pkg_ext), _d(version), _d(release))
        else:
            code = -1
            msg = u'%s v%s-%s 卸载失败！<p style="margin:10px">%s</p>' % \
                (_d(pkg_ext), _d(version), _d(release), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)
        self._unlock_job('yum')

    @tornado.gen.engine
    def yum_update(self, repo, pkg, version, release, ext):
        """Update a package.
        
        The parameter repo and version here are only for showing.
        """
        jobname = 'yum_update_%s_%s_%s_%s_%s' % (repo, pkg, ext, version, release)
        jobname = jobname.strip('_').replace('__', '_')
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，更新失败。')
            return

        if ext:
            self._update_job(jobname, 2, u'正在下载并升级扩展包，请耐心等候...')
        else:
            self._update_job(jobname, 2, u'正在下载并升级软件包，请耐心等候...')

        pkg_ext = ext and ext or pkg

        arch = self.settings['arch']
        if pkg_ext in yum.yum_pkg_noarchitecture:
            arch = 'noarch'

        cmd = 'yum update -y %s-%s-%s.%s' % (pkg_ext, version, release, arch)
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'成功升级 %s 到版本 v%s-%s！' % (_d(pkg_ext), _d(version), _d(release))
        else:
            code = -1
            msg = u'%s 升级到版本 v%s-%s 失败！<p style="margin:10px">%s</p>' % \
                (_d(pkg_ext), _d(version), _d(release), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)
        self._unlock_job('yum')

    @tornado.gen.engine
    def yum_ext_info(self, pkg):
        """Get ext info list of a pkg info.
        """
        jobname = 'yum_ext_info_%s' % pkg
        if not self._start_job(jobname): return
        if not self._lock_job('yum'):
            self._finish_job(jobname, -1, u'已有一个YUM进程在运行，获取扩展信息失败。')
            return
 
        self._update_job(jobname, 2, u'正在收集扩展信息...')

        exts = [k for k, v in yum.yum_pkg_relatives[pkg].items() if 'isext' in v and v['isext']]
        cmd = 'yum info %s --disableplugin=fastestmirror' % (' '.join(['%s.%s' % (ext, self.settings['arch']) for ext in exts]))

        data = []
        matched = False
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            matched = True
            lines = output.split('\n')
            for line in lines:
                if any(line.startswith(word)
                    for word in ('Name', 'Version', 'Release', 'Size',
                                 'Repo', 'From repo')):
                    fields = line.strip().split(':', 1)
                    if len(fields) != 2: continue
                    field_name = fields[0].strip().lower().replace(' ', '_')
                    field_value = fields[1].strip()
                    if field_name == 'name': data.append({})
                    data[-1][field_name] = field_value
        if matched:
            code = 0
            msg = u'获取扩展信息成功！'
        else:
            code = -1
            msg = u'获取扩展信息失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg, data)
        self._unlock_job('yum')
        
    @tornado.gen.engine
    def copy(self, srcpath, despath):
        """Copy a directory or file to a new path.
        """
        jobname = 'copy_%s_%s' % (srcpath, despath)
        if not self._start_job(jobname): return
 
        self._update_job(jobname, 2, u'正在复制 %s 到 %s...' % (_d(srcpath), _d(despath)))

        cmd = 'cp -rf %s %s' % (quote(srcpath), quote(despath))
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd, shell='*' in srcpath)
        if result == 0:
            code = 0
            msg = u'复制 %s 到 %s 完成！' % (_d(srcpath), _d(despath))
        else:
            code = -1
            msg = u'复制 %s 到 %s 失败！<p style="margin:10px">%s</p>' % (_d(srcpath), _d(despath), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)
        
    @tornado.gen.engine
    def move(self, srcpath, despath):
        """Move a directory or file recursively to a new path.
        """
        jobname = 'move_%s_%s' % (srcpath, despath)
        if not self._start_job(jobname): return
 
        self._update_job(jobname, 2, u'正在移动 %s 到 %s...' % (_d(srcpath), _d(despath)))
        
        # check if the despath exists
        # if exists, we first copy srcpath to despath, then remove the srcpath
        despath_exists = exists(despath)

        shell = False
        if despath_exists:
            # secure check
            if not exists(srcpath):
                self._finish_job(jobname, -1, u'不可识别的源！')
                return
            cmd = 'cp -rf %s/* %s' % (quote(srcpath), quote(despath))
            shell = True
        else:
            cmd = 'mv %s %s' % (quote(srcpath), quote(despath))
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd, shell=shell)
        if result == 0:
            code = 0
            msg = u'移动 %s 到 %s 完成！' % (_d(srcpath), _d(despath))
        else:
            code = -1
            msg = u'移动 %s 到 %s 失败！<p style="margin:10px">%s</p>' % (_d(srcpath), _d(despath), _d(output.strip().replace('\n', '<br>')))

        if despath_exists and code == 0:
            # remove the srcpath
            cmd = 'rm -rf %s' % (quote(srcpath), )
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result == 0:
                code = 0
                msg = u'移动 %s 到 %s 完成！' % (_d(srcpath), _d(despath))
            else:
                code = -1
                msg = u'移动 %s 到 %s 失败！<p style="margin:10px">%s</p>' % (_d(srcpath), _d(despath), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def remove(self, paths):
        """Remove a directory or file recursively.
        """
        jobname = 'remove_%s' % ','.join(paths)
        if not self._start_job(jobname): return
 
        for path in paths:
            self._update_job(jobname, 2, u'正在删除 %s...' % _d(path))
            cmd = 'rm -rf %s' % (quote(path))
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result == 0:
                code = 0
                msg = u'删除 %s 成功！' % _d(path)
            else:
                code = -1
                msg = u'删除 %s 失败！<p style="margin:10px">%s</p>' % (_d(path), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def compress(self, zippath, paths):
        """Compress files or directorys.
        """
        jobname = 'compress_%s_%s' % (zippath, ','.join(paths))
        if not self._start_job(jobname): return
        self._update_job(jobname, 2, u'正在压缩生成 %s...' % _d(zippath))

        shell = False

        basepath = dirname(zippath) + '/'
        path = ' '.join([quote(item.replace(basepath, '')) for item in paths])
        if zippath.endswith('.tar.gz') or zippath.endswith('.tgz'):
            cmd = 'tar zcf %s -C %s %s' % (quote(zippath), quote(basepath), path)
        elif zippath.endswith('.tar.bz2'):
            cmd = 'tar jcf %s -C %s %s' % (quote(zippath), quote(basepath), path)
        elif zippath.endswith('.zip'):
            if not exists('/usr/bin/zip'):
                self._update_job(jobname, 2, u'正在安装 zip...')
                if self.settings['dist_name'] in ('centos', 'redhat'):
                    cmd = 'yum install -y zip unzip'
                    result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
                    if result == 0:
                        self._update_job(jobname, 0, u'zip 安装成功！')
                    else:
                        self._update_job(jobname, -1, u'zip 安装失败！')
                        return
            cmd = 'cd %s; zip -rq9 %s %s' % (quote(basepath), quote(zippath), path)
            shell = True
        elif zippath.endswith('.gz'):
            path = ' '.join([quote(item) for item in paths])
            cmd = 'gzip -f %s' % path
        else:
            self._finish_job(jobname, -1, u'不支持的类型！')
            return

        result, output = yield tornado.gen.Task(call_subprocess, self, cmd, shell=shell)
        if result == 0:
            code = 0
            msg = u'压缩到 %s 成功！' % _d(zippath)
        else:
            code = -1
            msg = u'压缩失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def decompress(self, zippath, despath):
        """Decompress a zip file.
        """
        jobname = 'decompress_%s_%s' % (zippath, despath)
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在解压 %s...' % _d(zippath))
        if zippath.endswith('.tar.gz') or zippath.endswith('.tgz'):
            cmd = 'tar zxf %s -C %s' % (quote(zippath), quote(despath))
        elif zippath.endswith('.tar.bz2'):
            cmd = 'tar jxf %s -C %s' % (quote(zippath), quote(despath))
        elif zippath.endswith('.zip'):
            if not exists('/usr/bin/unzip'):
                self._update_job(jobname, 2, u'正在安装 unzip...')
                if self.settings['dist_name'] in ('centos', 'redhat'):
                    cmd = 'yum install -y zip unzip'
                    result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
                    if result == 0:
                        self._update_job(jobname, 0, u'unzip 安装成功！')
                    else:
                        self._update_job(jobname, -1, u'unzip 安装失败！')
                        return
            cmd = 'unzip -q -o %s -d %s' % (quote(zippath), quote(despath))
        elif zippath.endswith('.gz'):
            cmd = 'gunzip -f %s' % quote(zippath)
        else:
            self._finish_job(jobname, -1, u'不支持的类型！')
            return

        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'解压 %s 成功！' % _d(zippath)
        else:
            code = -1
            msg = u'解压 %s 失败！<p style="margin:10px">%s</p>' % (_d(zippath), _d(output.strip().replace('\n', '<br>')))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def chown(self, paths, user, group, option):
        """Change owner of paths.
        """
        jobname = 'chown_%s' % ','.join(paths)
        if not self._start_job(jobname):
            return

        self._update_job(jobname, 2, u'正在设置用户和用户组...')

        #cmd = 'chown %s %s:%s %s' % (option, user, group, ' '.join(paths))

        for path in paths:
            result = yield tornado.gen.Task(callbackable(files.chown), path, user, group, option=='-R')
            if result == True:
                code = 0
                msg = u'设置用户和用户组成功！'
            else:
                code = -1
                msg = u'设置 %s 的用户和用户组时失败！' % _d(path)
                break

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def chmod(self, paths, perms, option):
        """Change perms of paths.
        """
        jobname = 'chmod_%s' % ','.join(paths)
        if not self._start_job(jobname):
            return

        self._update_job(jobname, 2, u'正在设置权限...')

        #cmd = 'chmod %s %s %s' % (option, perms, ' '.join(paths))
        try:
            perms = int(perms, 8)
        except:
            self._finish_job(jobname, -1, u'权限值输入有误！')
            return

        for path in paths:
            result = yield tornado.gen.Task(callbackable(files.chmod), path, perms, option=='-R')
            if result == True:
                code = 0
                msg = u'权限修改成功！'
            else:
                code = -1
                msg = u'修改 %s 的权限时失败！' % _d(path)
                break

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def wget(self, url, path):
        """Run wget command to download file.
        """
        jobname = 'wget_%s' % tornado.escape.url_escape(url)
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在下载 %s...' % _d(url))

        if isdir(path): # download to the directory
            cmd = 'wget -q "%s" --directory-prefix=%s' % (quote(url), quote(path))
        else:
            cmd = 'wget -q "%s" -O %s' % (quote(url), quote(path))
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result == 0:
            code = 0
            msg = u'下载成功！'
        else:
            code = -1
            msg = u'下载失败！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_fupdatepwd(self, password):
        """Force updating mysql root password.
        """
        jobname = 'mysql_fupdatepwd'
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在检测 MySQL 服务状态...')
        cmd = 'service mysqld status'
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        isstopped = 'stopped' in output

        if not isstopped:
            self._update_job(jobname, 2, u'正在停止 MySQL 服务...')
            cmd = 'service mysqld stop'
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result != 0:
                self._finish_job(jobname, -1, u'停止 MySQL 服务时出错！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>')))
                return

        self._update_job(jobname, 2, u'正在启用 MySQL 恢复模式...')
        manually = False
        cmd = 'service mysqld startsos'
        result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        if result != 0:
            # some version of mysqld init.d script may not have startsos option
            # we run it manually
            manually = True
            cmd = 'mysqld_safe --skip-grant-tables --skip-networking'
            p = Popen(cmd, stdout=PIPE, stderr=STDOUT, close_fds=True, shell=True)
            if not p:
                self._finish_job(jobname, -1, u'启用 MySQL 恢复模式时出错！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>')))
                return

        # wait for the mysqld_safe to start up
        if manually: time.sleep(2)

        error = False
        self._update_job(jobname, 2, u'正在强制重置 root 密码...')
        if not mysql.fupdatepwd(password):
            error = True

        if manually:
            # 'service mysqld restart' cannot stop the manually start-up mysqld_safe process
            result = yield tornado.gen.Task(callbackable(mysql.shutdown), password)
            if result:
                self._update_job(jobname, 0, u'成功停止 MySQL 服务！')
            else:
                self._update_job(jobname, -1, u'停止 MySQL 服务失败！')
            p.terminate()
            p.wait()

        msg = ''
        if not isstopped:
            if error:
                msg = u'重置 root 密码时发生错误！正在重启 MySQL 服务...'
                self._update_job(jobname, -1, msg)
            else:
                self._update_job(jobname, 2, u'正在重启 MySQL 服务...')
            if manually:
                cmd = 'service mysqld start'
            else:
                cmd = 'service mysqld restart'
        else:
            if error:
                msg = u'重置 root 密码时发生错误！正在停止 MySQL 服务...'
                self._update_job(jobname, -1, msg)
            else:
                self._update_job(jobname, 2, u'正在停止 MySQL 服务...')
            if manually:
                cmd = ''
            else:
                cmd = 'service mysqld stop'

        if not cmd:
            if error:
                code = -1
                msg = u'%sOK' % msg
            else:
                code = 0
                msg = u'root 密码重置成功！'
        else:
            result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
            if result == 0:
                if error:
                    code = -1
                    msg = u'%sOK' % msg
                else:
                    code = 0
                    msg = u'root 密码重置成功！'
            else:
                if error:
                    code = -1
                    msg = u'%sOK' % msg
                else:
                    code = -1
                    msg = u'root 密码重置成功，但在操作服务时出错！<p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_databases(self, password):
        """Show MySQL database list.
        """
        jobname = 'mysql_databases'
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在获取数据库列表...')
        dbs = []
        dbs = yield tornado.gen.Task(callbackable(mysql.show_databases), password)
        if dbs:
            code = 0
            msg = u'获取数据库列表成功！'
        else:
            code = -1
            msg = u'获取数据库列表失败！'

        self._finish_job(jobname, code, msg, dbs)
    
    @tornado.gen.engine
    def mysql_users(self, password, dbname=None):
        """Show MySQL user list.
        """
        if not dbname:
            jobname = 'mysql_users'
        else:
            jobname = 'mysql_users_%s' % dbname
        if not self._start_job(jobname): return

        if not dbname:
            self._update_job(jobname, 2, u'正在获取用户列表...')
        else:
            self._update_job(jobname, 2, u'正在获取数据库 %s 的用户列表...' % _d(dbname))

        users = []
        users = yield tornado.gen.Task(callbackable(mysql.show_users), password, dbname)
        if users:
            code = 0
            msg = u'获取用户列表成功！'
        else:
            code = -1
            msg = u'获取用户列表失败！'

        self._finish_job(jobname, code, msg, users)
    
    @tornado.gen.engine
    def mysql_dbinfo(self, password, dbname):
        """Get MySQL database info.
        """
        jobname = 'mysql_dbinfo_%s' % dbname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在获取数据库 %s 的信息...' % _d(dbname))
        dbinfo = False
        dbinfo = yield tornado.gen.Task(callbackable(mysql.show_database), password, dbname)
        if dbinfo:
            code = 0
            msg = u'获取数据库 %s 的信息成功！' % _d(dbname)
        else:
            code = -1
            msg = u'获取数据库 %s 的信息失败！' % _d(dbname)

        self._finish_job(jobname, code, msg, dbinfo)
    
    @tornado.gen.engine
    def mysql_rename(self, password, dbname, newname):
        """MySQL database rename.
        """
        jobname = 'mysql_rename_%s' % dbname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在重命名 %s...' % _d(dbname))
        result = yield tornado.gen.Task(callbackable(mysql.rename_database), password, dbname, newname)
        if result == True:
            code = 0
            msg = u'%s 重命名成功！' % _d(dbname)
        else:
            code = -1
            msg = u'%s 重命名失败！' % _d(dbname)

        self._finish_job(jobname, code, msg)
    
    @tornado.gen.engine
    def mysql_create(self, password, dbname, collation):
        """Create MySQL database.
        """
        jobname = 'mysql_create_%s' % dbname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在创建 %s...' % _d(dbname))
        result = yield tornado.gen.Task(callbackable(mysql.create_database), password, dbname, collation=collation)
        if result == True:
            code = 0
            msg = u'%s 创建成功！' % _d(dbname)
        else:
            code = -1
            msg = u'%s 创建失败！' % _d(dbname)

        self._finish_job(jobname, code, msg)
    
    @tornado.gen.engine
    def mysql_export(self, password, dbname, path):
        """MySQL database export.
        """
        jobname = 'mysql_export_%s' % dbname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在导出 %s...' % _d(dbname))
        result = yield tornado.gen.Task(callbackable(mysql.export_database), password, dbname, path)
        if result == True:
            code = 0
            msg = u'%s 导出成功！' % _d(dbname)
        else:
            code = -1
            msg = u'%s 导出失败！' % _d(dbname)

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_drop(self, password, dbname):
        """Drop a MySQL database.
        """
        jobname = 'mysql_drop_%s' % dbname
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在删除 %s...' % _d(dbname))
        result = yield tornado.gen.Task(callbackable(mysql.drop_database), password, dbname)
        if result == True:
            code = 0
            msg = u'%s 删除成功！' % _d(dbname)
        else:
            code = -1
            msg = u'%s 删除失败！' % _d(dbname)

        self._finish_job(jobname, code, msg)
    
    @tornado.gen.engine
    def mysql_createuser(self, password, user, host, pwd=None):
        """Create MySQL user.
        """
        username = '%s@%s' % (user, host)
        jobname = 'mysql_createuser_%s' % username
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在添加用户 %s...' % _d(username))
        result = yield tornado.gen.Task(callbackable(mysql.create_user), password, user, host, pwd)
        if result == True:
            code = 0
            msg = u'用户 %s 添加成功！' % _d(username)
        else:
            code = -1
            msg = u'用户 %s 添加失败！' % _d(username)

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_userprivs(self, password, user, host):
        """Get MySQL user privileges.
        """
        username = '%s@%s' % (user, host)
        jobname = 'mysql_userprivs_%s' % username
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在获取用户 %s 的权限...' % _d(username))
        
        privs = {'global':{}, 'bydb':{}}
        globalprivs = yield tornado.gen.Task(callbackable(mysql.show_user_globalprivs), password, user, host)
        if globalprivs != False:
            code = 0
            msg = u'获取用户 %s 的全局权限成功！' % _d(username)
            privs['global'] = globalprivs
        else:
            code = -1
            msg = u'获取用户 %s 的全局权限失败！' % _d(username)
            privs = False
        
        if privs:
            dbprivs = yield tornado.gen.Task(callbackable(mysql.show_user_dbprivs), password, user, host)
            if dbprivs != False:
                code = 0
                msg = u'获取用户 %s 的数据库权限成功！' % _d(username)
                privs['bydb'] = dbprivs
            else:
                code = -1
                msg = u'获取用户 %s 的数据库权限失败！' % _d(username)
                privs = False

        self._finish_job(jobname, code, msg, privs)

    @tornado.gen.engine
    def mysql_updateuserprivs(self, password, user, host, privs, dbname=None):
        """Update MySQL user privileges.
        """
        username = '%s@%s' % (user, host)
        if dbname:
            jobname = 'mysql_updateuserprivs_%s_%s' % (username, dbname)
        else:
            jobname = 'mysql_updateuserprivs_%s' % username
        if not self._start_job(jobname): return

        if dbname:
            self._update_job(jobname, 2, u'正在更新用户 %s 在数据库 %s 中的权限...' % (_d(username), _d(dbname)))
        else:
            self._update_job(jobname, 2, u'正在更新用户 %s 的权限...' % _d(username))
            
        rt = yield tornado.gen.Task(callbackable(mysql.update_user_privs), password, user, host, privs, dbname)
        if rt != False:
            code = 0
            msg = u'用户 %s 的权限更新成功！' % _d(username)
        else:
            code = -1
            msg = u'用户 %s 的权限更新失败！' % _d(username)

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_setuserpassword(self, password, user, host, pwd):
        """Set password of MySQL user.
        """
        username = '%s@%s' % (user, host)
        jobname = 'mysql_setuserpassword_%s' % username
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在更新用户 %s 的密码...' % _d(username))
            
        rt = yield tornado.gen.Task(callbackable(mysql.set_user_password), password, user, host, pwd)
        if rt != False:
            code = 0
            msg = u'用户 %s 的密码更新成功！' % _d(username)
        else:
            code = -1
            msg = u'用户 %s 的密码更新失败！' % _d(username)

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def mysql_dropuser(self, password, user, host):
        """Drop a MySQL user.
        """
        username = '%s@%s' % (user, host)
        jobname = 'mysql_dropuser_%s' % username
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在删除用户 %s...' % _d(username))
            
        rt = yield tornado.gen.Task(callbackable(mysql.drop_user), password, user, host)
        if rt != False:
            code = 0
            msg = u'用户 %s 删除成功！' % _d(username)
        else:
            code = -1
            msg = u'用户 %s 删除失败！' % _d(username)

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def ssh_genkey(self, path, password=''):
        """Generate a ssh key pair.
        """
        jobname = 'ssh_genkey'
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在生成密钥对...')
            
        rt = yield tornado.gen.Task(callbackable(ssh.genkey), path, password)
        if rt != False:
            code = 0
            msg = u'密钥对生成成功！'
        else:
            code = -1
            msg = u'密钥对生成失败！'

        self._finish_job(jobname, code, msg)

    @tornado.gen.engine
    def ssh_chpasswd(self, path, oldpassword, newpassword=''):
        """Change password of a ssh private key.
        """
        jobname = 'ssh_chpasswd'
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在修改私钥密码...')
            
        rt = yield tornado.gen.Task(callbackable(ssh.chpasswd), path, oldpassword, newpassword)
        if rt != False:
            code = 0
            msg = u'私钥密码修改成功！'
        else:
            code = -1
            msg = u'私钥密码修改失败！'

        self._finish_job(jobname, code, msg)

    @tornado.web.asynchronous
    @tornado.gen.engine
    def inpanel_install(self, ssh_ip, ssh_port, ssh_user, ssh_password, instance_name, accessnet, accessport=None, accesskey=None):
        """Install InPanel"""
        jobname = 'inpanel_install_%s' % ssh_ip
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在将 InPanel 安装到 %s...' % ssh_ip)

        result = yield tornado.gen.Task(callbackable(remote.inpanel_install),
                    ssh_ip, ssh_port, ssh_user, ssh_password, accesskey=accesskey, inpanel_port=accessport)
        if result == True:
            code = 0
            msg = u'InPanel 安装成功！'
            self.config.set('inpanel', instance_name, '%s|%s|%s' % (accesskey, accessnet, accessport))
        else:
            code = -1
            msg = u'InPanel 安装过程中发生错误！'

        self._finish_job(jobname, code, msg)

    @tornado.web.asynchronous
    @tornado.gen.engine
    def inpanel_uninstall(self, ssh_ip, ssh_port, ssh_user, ssh_password, instance_name):
        """Uninstall InPanel"""
        jobname = 'inpanel_uninstall_%s' % ssh_ip
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在卸载 %s 上的 InPanel...' % ssh_ip)
        result = yield tornado.gen.Task(callbackable(remote.inpanel_uninstall),
                    ssh_ip, ssh_port, ssh_user, ssh_password)
        if result == True:
            code = 0
            msg = u'InPanel 卸载成功！'
            try:
                self.config.remove_option('inpanel', instance_name)
            except:
                pass
        else:
            code = -1
            msg = u'InPanel 卸载过程中发生错误！'

        self._finish_job(jobname, code, msg)

    @tornado.web.asynchronous
    @tornado.gen.engine
    def inpanel_config(self, ssh_ip, ssh_port, ssh_user, ssh_password, accesskey=None):
        """Update InPanel Config"""
        jobname = 'inpanel_config%s' % ssh_ip
        if not self._start_job(jobname): return

        self._update_job(jobname, 2, u'正在更新 %s 上的 InPanel 配置...' % ssh_ip)

        result = yield tornado.gen.Task(callbackable(remote.inpanel_config),
                    ssh_ip, ssh_port, ssh_user, ssh_password, accesskey=accesskey)
        if result == True:
            code = 0
            msg = u'InPanel 配置更新成功！'
        else:
            code = -1
            msg = u'InPanel 配置更新过程中发生错误！'

        self._finish_job(jobname, code, msg)

    # @tornado.web.asynchronous
    @tornado.gen.engine
    def uploadtoftp(self, address, account, password, source, target):
        '''transmit files to ftp server.'''
        jobname = 'uploadtoftp_%s_%s_%s' % (address, source, target)
        # jobname = 'uploadtoftp_%s' % address
        if not self._start_job(jobname): return

        if not isfile(source):
            self._finish_job(jobname, -1, '传输失败！文件不存在')
            return

        self._update_job(jobname, 2, u'正在传输文件 %s...' % source)
        result = yield tornado.gen.Task(callbackable(ftp.uploadtoftp), address, account, password, source, target)
        # result = yield tornado.gen.Task(callbackable(ftp.uploadtoftpa), address, account, password, source, target)
        # cmd = 'ping www.baidu.com -c 8'
        # result, output = yield tornado.gen.Task(call_subprocess, self, cmd)
        # print(result)
        if result == True:
            code = 0
            msg = u'文件 %s 已成功传输到 %s 服务器！' % (source, address)
        else:
            code = -1
            msg = u'文件传输失败！' # <p style="margin:10px">%s</p>' % _d(output.strip().replace('\n', '<br>'))
        self._finish_job(jobname, code, msg)


class BackupHandler(RequestHandler):
    def get(self):
        self.authed()

        if self.config.get('runtime', 'mode') == 'demo':
            self.write(u'DEMO状态不允许执行此操作！')
            return

        path = joinpath(self.settings['data_path'], 'config.ini')
        if isfile(path):
            self.set_header('Content-Type', 'application/octet-stream')
            self.set_header('Content-disposition', 'attachment; filename=inpanel_backup_%s.bak' % time.strftime('%Y%m%d'))
            self.set_header('Content-Transfer-Encoding', 'binary')
            with open(path) as f: self.write(f.read())
        else:
            self.write(u'配置文件不存在！')

    def authed(self):
        # check for the access token
        access_token = (self.get_argument("_access", None) or self.request.headers.get("X-ACCESS-TOKEN"))
        if access_token and self.config.get('auth', 'accesskeyenable') == 'on':
            if access_token != self.config.get('auth', 'accesskey'):
                raise tornado.web.HTTPError(403, 'Access Token Error')
        else:
            # check the cookie within 30 mins
            if self.get_secure_cookie('authed', None, 30.0/1440) == 'yes':
                # regenerate the cookie timestamp per 5 mins
                if self.get_secure_cookie('authed', None, 5.0/1440) == None:
                    self.set_secure_cookie('authed', 'yes', None)
            else:
                raise tornado.web.HTTPError(403, "Please Login First")


class RestoreHandler(RequestHandler):
    def post(self):
        self.authed()

        if self.config.get('runtime', 'mode') == 'demo':
            self.write(u'DEMO状态不允许执行此操作！')
            return

        path = joinpath(self.settings['data_path'], 'config.ini')

        self.write(u'<body style="font-size:14px;overflow:hidden;margin:0;padding:0;">')

        if not 'ufile' in self.request.files:
            self.write(u'请选择备份配置文件！')
        else:
            self.write(u'正在上传...')
            file = self.request.files['ufile'][0]
            testpath = path+'.test'
            with open(testpath, 'wb') as f: f.write(file['body'])

            try:
                t = configurations(testpath)
                with open(path, 'wb') as f: f.write(file['body'])
                self.write(u'还原成功！')
            except:
                self.write(u'配置文件有误，还原失败！')

            unlink(testpath)

        self.write('</body>')


class BuyECSHandler(RequestHandler):
    """Aliyun CPS program.
    """
    def get(self):
        self.redirect('http://www.aliyun.com/cps/rebate?from_uid=zop0qMW4KbY=')


class AccountHandler(RequestHandler):
    """ECS Account handler.
    """
    def get(self):
        self.authed()
        status = self.get_argument('status', '')

        accounts = self.config.get('ecs', 'accounts')
        try:
            accounts = loads(accounts)
        except:
            accounts = []

        accounts = sorted(accounts, key=lambda k:k['name'])
        if status:
            status = status == 'enable'
            accounts = filter(lambda a: a['status'] == status, accounts)

        if self.config.get('runtime', 'mode') == 'demo':
            for i, account in enumerate(accounts):
                accounts[i]['access_key_secret'] = '***DEMO状态下密钥被保护***'

        self.write({'code': 0, 'msg': u'成功加载 ECS 帐号列表！', 'data': accounts})

    def post(self):
        self.authed()
        action = self.get_argument('action', '')

        if self.config.get('runtime', 'mode') == 'demo':
            self.write({'code': -1, 'msg': u'DEMO状态不允许修改 ECS 帐号！'})
            return

        if action == 'add' or action == 'update':
            name = self.get_argument('name', '')
            access_key_id = self.get_argument('access_key_id', '')
            access_key_secret = self.get_argument('access_key_secret', '')
            status = self.get_argument('status', '')
            newaccount = {
                'name': name,
                'access_key_id': access_key_id,
                'access_key_secret': access_key_secret,
                'status': status == 'on',
            }
            if action == 'update':
                old_access_key_id = self.get_argument('old_access_key_id', '')

            accounts = self.config.get('ecs', 'accounts')
            try:
                accounts = loads(accounts)
            except:
                accounts = []

            if action == 'add':
                for account in accounts:
                    if account['access_key_id'] == access_key_id:
                        self.write({'code': -1, 'msg': u'添加失败！该 Access Key ID 已存在！'})
                        return
                accounts.append(newaccount)
            else:
                found = False
                for i, account in enumerate(accounts):
                    if account['access_key_id'] == old_access_key_id:
                        accounts[i] = newaccount
                        found = True
                        break
                if not found:
                    self.write({'code': -1, 'msg': u'更新失败！该 Access Key ID 不存在！'})
                    return

            self.config.set('ecs', 'accounts', dumps(accounts))
            if action == 'add':
                self.write({'code': 0, 'msg': u'新帐号添加成功！'})
            else:
                self.write({'code': 0, 'msg': u'帐号更新成功！'})

        elif action == 'delete':
            access_key_id = self.get_argument('access_key_id', '')
            accounts = self.config.get('ecs', 'accounts')
            try:
                accounts = loads(accounts)
            except:
                accounts = []

            found = False
            for i, account in enumerate(accounts):
                if account['access_key_id'] == access_key_id:
                    del accounts[i]
                    found = True
                    break
            if not found:
                self.write({'code': -1, 'msg': u'删除失败！该 Access Key ID 不存在！'})
                return

            self.config.set('ecs', 'accounts', dumps(accounts))
            self.write({'code': 0, 'msg': u'帐号删除成功！'})


class ECSHandler(RequestHandler):
    '''ECS operation handler.'''

    def _get_secret(self, access_key_id):
        accounts = self.config.get('ecs', 'accounts')
        try:
            accounts = loads(accounts)
        except:
            accounts = []

        for account in accounts:
            if account['access_key_id'] == access_key_id:
                return account['access_key_secret']

        return False

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, section):
        self.authed()

        if section == 'instances':
            access_key_id = self.get_argument('access_key_id', '')
            page_number = self.get_argument('page_number', '1')
            page_size = self.get_argument('page_size', '10')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            result, data, reqid = yield tornado.gen.Task(callbackable(srv.DescribeInstanceStatus), PageNumber=_u(page_number), PageSize=_u(page_size))
            if not result:
                self.write({'code': -1, 'msg': u'云服务器列表加载失败！（%s）' % data['Message']})
                self.finish()
                return

            instances = []
            tasks = []
            if 'InstanceStatusSets' in data:
                for instance in data['InstanceStatusSets']:
                    tasks.append(tornado.gen.Task(callbackable(srv.DescribeInstanceAttribute), _u(instance['InstanceName'])))
                    instances.append(instance)

            if tasks:
                responses = yield tasks
                for i, response in enumerate(responses):
                    result, instdata, reqid = response
                    if result: instances[i].update(instdata)

            # get access info for InPanel
            for instance in instances:
                if not self.config.has_option('inpanel', instance['InstanceName']):
                    instance['InPanelStatus'] = False
                else:
                    accessdata = self.config.get('inpanel', instance['InstanceName'])
                    accessdata = accessdata.split('|')
                    accessinfo = {
                        'accesskey': accessdata[0],
                        'accessnet': accessdata[1],
                        'accessport': accessdata[2],
                    }
                    instance['InPanelStatus'] = accessinfo

            self.write({'code': 0, 'msg': u'成功加载云服务器列表！', 'data': {
                'instances': instances,
                'page_number': data['PageNumber'],
                'page_size': data['PageSize'],
            }})
            self.finish()

        elif section == 'instance':
            access_key_id = self.get_argument('access_key_id', '')
            instance_name = self.get_argument('instance_name', '')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            result, instdata, reqid = yield tornado.gen.Task(callbackable(srv.DescribeInstanceAttribute), _u(instance_name))
            if not result:
                self.write({'code': -1, 'msg': u'云服务器 %s 信息加载失败！（%s）' % (instance_name, instdata['Message'])})
                self.finish()
                return

            self.write({'code': 0, 'msg': u'成功加载云服务器信息！', 'data': instdata})
            self.finish()

        elif section == 'images':
            access_key_id = self.get_argument('access_key_id', '')
            region_code = self.get_argument('region_code', '')
            page_number = self.get_argument('page_number', '1')
            page_size = self.get_argument('page_size', '10')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            result, data, reqid = yield tornado.gen.Task(callbackable(srv.DescribeImages), RegionCode=_u(region_code), PageNumber=_u(page_number), PageSize=_u(page_size))
            if not result:
                self.write({'code': -1, 'msg': u'系统镜像列表加载失败！（%s）' % data['Message']})
                self.finish()
                return

            if 'Images' in data:
                images = data['Images']
            else:
                images = []

            self.write({'code': 0, 'msg': u'成功加载系统镜像列表！', 'data': {
                'images': images,
                'total_number': data['ImageTotalNumber'],
                'page_number': data['PageNumber'],
                'page_size': data['PageSize'],
            }})
            self.finish()

        elif section == 'disks':
            access_key_id = self.get_argument('access_key_id', '')
            instance_name = self.get_argument('instance_name', '')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            result, data, reqid = yield tornado.gen.Task(callbackable(srv.DescribeDisks), InstanceName=_u(instance_name))
            if not result:
                self.write({'code': -1, 'msg': u'磁盘列表加载失败！（%s）' % data['Message']})
                self.finish()
                return

            if 'Disks' in data:
                disks = data['Disks']
            else:
                disks = []

            self.write({'code': 0, 'msg': u'成功加载磁盘列表列表！', 'data': {
                'disks': disks
            }})
            self.finish()

        elif section == 'snapshots':
            access_key_id = self.get_argument('access_key_id', '')
            instance_name = self.get_argument('instance_name', '')
            disk_code = self.get_argument('disk_code', '')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            result, data, reqid = yield tornado.gen.Task(callbackable(srv.DescribeSnapshots), InstanceName=_u(instance_name), DiskCode=_u(disk_code))
            if not result:
                self.write({'code': -1, 'msg': u'磁盘快照列表加载失败！（%s）' % data['Message']})
                self.finish()
                return

            if 'Snapshots' in data:
                snapshots = data['Snapshots']
            else:
                snapshots = []

            snapshots = sorted(snapshots, key=lambda k:k['CreateTime'], reverse=True)

            self.write({'code': 0, 'msg': u'成功加载磁盘快照列表！', 'data': {
                'snapshots': snapshots
            }})
            self.finish()

        elif section == 'accessinfo':
            instance_name = self.get_argument('instance_name', '')
            if not instance_name:
                self.write({'code': -1, 'msg': u'服务器不存在！'})
                self.finish()
                return

            if not self.config.has_option('inpanel', instance_name):
                accessinfo = {'accesskey': '', 'accessnet': 'public', 'accessport': '8888'}
            else:
                data = self.config.get('inpanel', instance_name)
                data = data.split('|')
                accessinfo = {
                    'accesskey': data[0],
                    'accessnet': data[1],
                    'accessport': data[2],
                }

            self.write({'code': 0, 'msg': u'', 'data': accessinfo})
            self.finish()

        else:
            self.write({'code': -1, 'msg': u'未定义的操作！'})
            self.finish()

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, section):
        self.authed()

        if section in ('startinstance', 'stopinstance', 'rebootinstance', 'resetinstance'):

            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                self.finish()
                return

            access_key_id = self.get_argument('access_key_id', '')
            instance_name = self.get_argument('instance_name', '')
            if section in ('stopinstance', 'rebootinstance'):
                force = self.get_argument('force', '') and 'true' or None
            elif section == 'resetinstance':
                image_code = self.get_argument('image_code', '')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            opstr = {'startinstance': u'启动', 'stopinstance': u'停止', 'rebootinstance': u'重启', 'resetinstance': u'重置'}

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            if section == 'startinstance':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.StartInstance), _u(instance_name))
            elif section == 'stopinstance':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.StopInstance), _u(instance_name), ForceStop=_u(force))
            elif section == 'rebootinstance':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.RebootInstance), _u(instance_name), ForceStop=_u(force))
            elif section == 'resetinstance':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.ResetInstance), _u(instance_name), ImageCode=_u(image_code))
            if not result:
                self.write({'code': -1, 'msg': u'云服务器 %s %s失败！（%s）' % (instance_name, opstr[section], data['Message'])})
                self.finish()
                return

            self.write({'code': 0, 'msg': u'云服务器%s指令发送成功！' % opstr[section], 'data': data})
            self.finish()

        elif section in ('createsnapshot', 'deletesnapshot', 'cancelsnapshot', 'rollbacksnapshot'):

            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                self.finish()
                return

            access_key_id = self.get_argument('access_key_id', '')
            instance_name = self.get_argument('instance_name', '')
            disk_code = self.get_argument('disk_code', '')
            if section in ('deletesnapshot', 'cancelsnapshot', 'rollbacksnapshot'):
                snapshot_code = self.get_argument('snapshot_code', '')

            access_key_secret = self._get_secret(access_key_id)
            if access_key_secret == False:
                self.write({'code': -1, 'msg': u'该帐号不存在！'})
                self.finish()
                return

            opstr = {'createsnapshot': u'创建', 'deletesnapshot': u'删除', 'cancelsnapshot': u'取消', 'rollbacksnapshot': u'回滚'}

            srv = aliyuncs.ECS(_u(access_key_id), _u(access_key_secret))
            if section == 'createsnapshot':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.CreateSnapshot), InstanceName=_u(instance_name), DiskCode=_u(disk_code))
            elif section == 'deletesnapshot':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.DeleteSnapshot), InstanceName=_u(instance_name), DiskCode=_u(disk_code), SnapshotCode=_u(snapshot_code))
            elif section == 'cancelsnapshot':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.CancelSnapshotRequest), InstanceName=_u(instance_name), SnapshotCode=_u(snapshot_code))
            elif section == 'rollbacksnapshot':
                result, data, reqid = yield tornado.gen.Task(callbackable(srv.RollbackSnapshot), InstanceName=_u(instance_name), DiskCode=_u(disk_code), SnapshotCode=_u(snapshot_code))
            if not result:
                self.write({'code': -1, 'msg': u'快照%s失败！（%s）' % (opstr[section], data['Message'])})
                self.finish()
                return

            self.write({'code': 0, 'msg': u'快照%s指令发送成功！' % opstr[section], 'data': data})
            self.finish()

        elif section == 'accessinfo':

            if self.config.get('runtime', 'mode') == 'demo':
                self.write({'code': -1, 'msg': u'DEMO状态不允许此类操作！'})
                self.finish()
                return

            instance_name = self.get_argument('instance_name', '')
            accesskey = self.get_argument('accesskey', '')
            accessnet = self.get_argument('accessnet', '')
            accessport = self.get_argument('accessport', '')

            if not instance_name:
                self.write({'code': -1, 'msg': u'服务器不存在！'})
                self.finish()
                return

            self.config.set('inpanel', instance_name, '%s|%s|%s' % (accesskey, accessnet, accessport))

            self.write({'code': 0, 'msg': u'InPanel 远程控制设置保存成功！'})
            self.finish()

        else:
            self.write({'code': -1, 'msg': u'未定义的操作！'})
            self.finish()


class InPanelIndexHandler(RequestHandler):
    """Index page of InPanel.
    """
    def get(self, instance_name, ip, port):
        with open(joinpath(self.settings['inpanel_path'], 'index.html')) as f:
            html = f.read()
        html = html.replace('<link rel="stylesheet" href="', '<link rel="stylesheet" href="/inpanel/')
        html = html.replace('<script src="', '<script src="/inpanel/')
        html = html.replace("var template_path = '';", "var template_path = '/inpanel';")
        html = html.replace("var releasetime = '';", "var releasetime = '%s';" % releasetime)
        self.write(html)


class InPanelHandler(RequestHandler):
    """Operation proxy of InPanel.

    REF: https://groups.google.com/forum/?fromgroups=#!topic/python-tornado/TB_6oKBmdlA
    """
    def handle_response(self, response): 
        if response.error and not isinstance(response.error, tornado.httpclient.HTTPError): 
            loginfo("response has error %s", response.error)
            self.set_status(500)
            self.write("Internal server error:\n" + str(response.error))
            self.finish()
        else:
            self.set_status(response.code)
            for header in ('Date', 'Cache-Control', 'Content-Type', 'Etag', 'Location'):
                v = response.headers.get(header)
                if v:
                    self.set_header(header, v)
            if response.body:
                self.write(response.body)
            self.finish()

    def forward(self, port=None, host=None): 
        try:
            tornado.httpclient.AsyncHTTPClient().fetch(
                tornado.httpclient.HTTPRequest(
                    url = "%s://%s:%s%s" % (
                        self.request.protocol, host or "127.0.0.1",
                        port or 80, self.request.uri),
                    method=self.request.method,
                    body=self.request.body,
                    headers=self.request.headers,
                    follow_redirects=False),
                self.handle_response)
        except tornado.httpclient.HTTPError as x:
            loginfo("tornado signalled HTTPError %s", x)
            if hasattr(x, 'response') and x.response:
                self.handle_response(x.response)
        except:
            self.set_status(500)
            self.write("Internal server error\n")
            self.finish()
    
    def gen_token(self, instance_name):
        if not self.config.has_option('inpanel', instance_name):
            self.set_status(403)
            self.finish()
            return
        else:
            data = self.config.get('inpanel', instance_name)
            data = data.split('|')
            accesskey = data[0]

        accesskey = b64decode(accesskey)
        key = accesskey[:24]
        iv = accesskey[24:]
        k = pyDes.triple_des(key, pyDes.CBC, iv, pad=None, padmode=pyDes.PAD_PKCS5)
        access_token = k.encrypt('timestamp:%d' % int(time.time()))
        access_token = b64encode(access_token)
        return access_token

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, instance_name, ip, port, uri):
        self.authed()
        self.request.body = None
        self.request.uri = '/'+uri
        self.request.headers['X-ACCESS-TOKEN'] = self.gen_token(instance_name)
        self.forward(port, ip)

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, instance_name, ip, port, uri):
        self.authed()
        self.request.uri = '/'+uri
        self.request.headers['X-ACCESS-TOKEN'] = self.gen_token(instance_name)
        self.forward(port, ip)
