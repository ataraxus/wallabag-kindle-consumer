import asyncio
import os

import aiohttp_jinja2
import jinja2
from aiohttp import web

from email_validator import validate_email, EmailNotValidError

from . import wallabag
from . import models


class Validator:
    def __init__(self, loop, data):
        self.loop = loop
        self.data = data
        self.errors = {}
        self.username = None
        self.password = None
        self.kindle_email = None
        self.notify_email = None

    async def validate_credentials(self):
        errors = {}
        if "username" not in self.data or 0 == len(self.data['username']):
            errors['username'] = 'Username not given or empty'
        else:
            self.username = self.data['username']

        if 'password' not in self.data or 0 == len(self.data['password']):
            errors['password'] = "Password not given or empty"
        else:
            self.password = self.data['password']

        self.errors.update(errors)
        return 0 == len(errors)

    async def _validate_email(self, address):
        val = await self.loop.run_in_executor(None, validate_email, address)
        return val['email']

    async def validate_emails(self):
        errors = {}
        if 'kindleEmail' not in self.data or 0 == len(self.data['kindleEmail']):
            errors['kindleEmail'] = "Kindle email address not given or empty"
        else:
            try:
                kindleEmail = await self._validate_email(self.data['kindleEmail'])
                if kindleEmail.endswith('@kindle.com') or kindleEmail.endswith('@free.kindle.com'):
                    self.kindle_email = kindleEmail
                else:
                    errors['kindleEmail'] = 'Given Kindle email does not end with @kindle.com or @free.kindle.com'
            except EmailNotValidError:
                errors['kindleEmail'] = "Kindle email is not a valid email address"

        if 'notifyEmail' not in self.data or 0 == len(self.data['notifyEmail']):
            errors['notifyEmail'] = "Notification email not given or empty"
        else:
            try:
                self.notify_email = await self._validate_email(self.data['notifyEmail'])
            except EmailNotValidError:
                errors['notifyEmail'] = "Notification email is not a valid email address"

        self.errors.update(errors)
        return 0 == len(errors)

    @property
    def success(self):
        return 0 == len(self.errors)


class ViewBase(web.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._errors = {}
        self._data = {}
        self._messages = []

    @property
    def _cfg(self):
        return self.request.app['config']

    @property
    def _wallabag(self):
        return self.request.app['wallabag']

    def _template(self, vars):
        vars.update({'errors': self._errors, 'data': self._data, 'messages': self._messages,
                     'wallabag_host': self._cfg.wallabag_host,
                     'tags': [t.tag for t in wallabag.make_tags(self._cfg.tag)]})
        return vars

    def _add_errors(self, errors):
        self._errors.update(errors)

    def _set_data(self, data):
        self._data = data

    def _add_message(self, msg):
        self._messages.append(msg)

    @property
    def _session(self):
        return self.request.app['session_maker']


class IndexView(ViewBase):
    @aiohttp_jinja2.template("index.html")
    async def get(self):
        return self._template({})

    @aiohttp_jinja2.template("index.html")
    async def post(self):
        data = await self.request.post()
        self._set_data(data)

        validator = Validator(self.request.app.loop, data)

        await asyncio.gather(validator.validate_emails(),
                             validator.validate_credentials())
        self._add_errors(validator.errors)

        if validator.success:
            user = models.User(name=validator.username, kindle_mail=validator.kindle_email,
                               email=validator.notify_email)

            with self._session as session:
                if session.query(models.User.name).filter(models.User.name == validator.username).count() != 0:
                    self._add_errors({'user': "User is already registered"})
                elif not await self._wallabag.get_token(user, validator.password):
                    self._add_errors({'auth': 'Cannot authenticate at wallabag server to get a token'})
                else:
                    session.add(user)
                    session.commit()
                    self._add_message("User successfully registered")
                    self._set_data({})

        return self._template({})


@aiohttp_jinja2.template("relogin.html")
def re_login(request):
    pass


@aiohttp_jinja2.template("index.html")
def delete_user(request):
    pass


class App:
    def __init__(self, config, wallabag):
        self.config = config
        self.wallabag = wallabag
        self.app = web.Application()

        self.setup_app()
        self.setup_routes()

    def setup_app(self):
        self.app['config'] = self.config
        self.app['wallabag'] = self.wallabag
        self.app['session_maker'] = models.context_session(self.config)
        aiohttp_jinja2.setup(
            self.app, loader=jinja2.PackageLoader('wallabag_kindle_consumer', 'templates'))

        self.app['static_root_url'] = '/static'

    def setup_routes(self):
        self.app.router.add_static('/static/',
                                   path=os.path.join(os.path.dirname(__file__), 'static'),
                                   name='static')
        self.app.router.add_view("/", IndexView)

    def run(self):
        web.run_app(self.app, host=self.config.interface_host, port=self.config.interface_port)

    async def register_server(self, loop):
        await loop.create_server(self.app.make_handler(),
                                 self.config.interface_host, self.config.interface_port)
