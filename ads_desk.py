import os
import redis
import datetime
from urllib.parse import urlparse
from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.wsgi import SharedDataMiddleware
from werkzeug.utils import redirect

from jinja2 import Environment, FileSystemLoader


def base36_encode(number):
    assert number >= 0, 'positive integer required'
    if number == 0:
        return '0'
    base36 = []
    while number != 0:
        number, i = divmod(number, 36)
        base36.append('0123456789abcdefghijklmnopqrstuvwxyz'[i])
    return ''.join(reversed(base36))


def get_hostname(url):
    return urlparse(url).netloc


class Shortly(object):

    def __init__(self, config):
        """
        Initializing
        :param config:
        """
        self.redis = redis.Redis(config['redis_host'], config['redis_port'])
        template_path = os.path.join(os.path.dirname(__file__), 'templates')
        self.jinja_env = Environment(loader=FileSystemLoader(template_path),
                                     autoescape=True)
        self.jinja_env.filters['hostname'] = get_hostname

        self.url_map = Map([
            Rule('/', endpoint='main'),
            Rule('/create_add', endpoint='create_add'),
            Rule('/board:<board_id>', endpoint='detail'),
            Rule('/comment:<board_id>', endpoint='comment')
        ])

    def on_create_add(self, request):
        """
        Creating advertisment
        :param request:
        :return: response(render)
        """
        error = None
        if request.method == 'POST':
            creator = request.form['creator']
            board_name = request.form['board_name']
            if len(creator) > 30:
                error = 'creator name too long'
            elif len(board_name) > 50:
                error = 'board name too long'
            else:
                self.insert_add(request)
                return redirect('/')
        return self.render_template('create_add.html', error=error)

    def insert_add(self, request):
        """
        Inserting advertisment
        :param request:
        :return: short_id
        """
        short_id = self.redis.get('board:' + request.form['board_name'])
        if short_id is not None:
            return short_id
        url_num = self.redis.incr('last-board-id')
        short_id = base36_encode(url_num)
        self.redis.set('board:' + short_id, request.form['board_name'])
        self.redis.set('creator:board:' + short_id, request.form['creator'])
        self.redis.set('time:board:' + short_id, datetime.datetime.now())
        return short_id

    def get_adds(self):
        """
        Get advertisment
        :return: list of advertismaents
        """
        keys = self.redis.keys('board:*')
        keys.sort()
        ads_list = []
        for i, key in enumerate(keys,1):
            ads_list.append((
                i,
                self.redis.get(key).decode('utf-8')
            ))
        return ads_list

    def on_comment(self, request, board_id):
        """
        React on atempt to make comment
        :param request:
        :param board_id:
        :return: response(render)
        """
        error = None
        if request.method == 'POST':
            creator = request.form['creator']
            comment = request.form['comment']
            if len(creator) > 30:
                error = 'creator name too long'
            elif len(comment) > 50:
                error = 'comment too long'
            else:
                self.insert_comment(request, board_id)
                return redirect('/board:' + board_id)
        return self.render_template('comment.html', error=error)

    def insert_comment(self, request, board_id):
        """
        Inserting comment
        :param request:
        :param board_id:
        :return: short_id
        """
        url_num = self.redis.incr('last-comment-id:')
        short_id = base36_encode(url_num)
        self.redis.set('comment:' + short_id, request.form['comment'])
        self.redis.set('creator:comment:' + short_id, request.form['creator'])
        self.redis.lpush('comment:board:' + board_id, short_id)
        return short_id

    def get_comments(self, board_id):
        """
        Getting comments
        :param board_id:
        :return: list of comments
        """
        lenght = self.redis.llen('comment:board:' + board_id)
        keys = []
        for i in range(lenght):
            keys.append(self.redis.lindex('comment:board:' + board_id, i).decode('utf-8'))
        keys.sort()
        comment_array = []
        for key in keys:
            comment_array.append({
                'creator': self.redis.get('creator:comment:' + key).decode('utf-8'),
                'comment': self.redis.get('comment:' + key).decode('utf-8')
            })
        return comment_array


    def on_main(self, request):
        """
        React on main page request
        :param request:
        :return: response(render)
        """
        return self.render_template('main.html', ads=self.get_adds())

    def on_detail(self, request, board_id):
        """
        React on call details of advertisment
        :param request:
        :param board_id:
        :return: response(render)
        """
        detailed_info = {
            'creator': self.redis.get('creator:board:' + board_id).decode('utf-8'),
            'text': self.redis.get('board:' + board_id).decode('utf-8'),
            'time': self.redis.get('time:board:' + board_id).decode('utf-8'),
            'board_id': board_id
        }
        return self.render_template('details.html', detailed_info=detailed_info, comments=self.get_comments(board_id))

    def error_404(self):
        """
        React on attempt to acces unavaliable page
        :return: response
        """
        response = self.render_template('404.html')
        response.status_code = 404
        return response

    def render_template(self, template_name, **context):
        """
        Rendering template
        :param template_name:
        :param context:
        :return: response(render)
        """
        t = self.jinja_env.get_template(template_name)
        return Response(t.render(context), mimetype='text/html')

    def dispatch_request(self, request):
        """
        Handling request
        :param request:
        :return: response
        """
        adapter = self.url_map.bind_to_environ(request.environ)
        try:
            endpoint, values = adapter.match()
            return getattr(self, 'on_' + endpoint)(request, **values)
        except NotFound as e:
            return self.error_404()
        except HTTPException as e:
            return e

    def wsgi_app(self, environ, start_response):
        """
        Creates request
        :param environ:
        :param start_response:
        :return: response
        """
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)


def create_app(redis_host='localhost', redis_port=6379, with_static=True):
    app = Shortly({
        'redis_host':       redis_host,
        'redis_port':       redis_port
    })
    if with_static:
        app.wsgi_app = SharedDataMiddleware(app.wsgi_app, {
            '/static':  os.path.join(os.path.dirname(__file__), 'static')
        })
    return app


if __name__ == '__main__':
    from werkzeug.serving import run_simple
    app = create_app()
    run_simple('127.0.0.1', 5000, app, use_debugger=True, use_reloader=True)
