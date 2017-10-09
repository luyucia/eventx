#encoding:utf8
import sys
reload(sys)
sys.setdefaultencoding('utf8')
import tornado.ioloop
import tornado.web
import json
import md5
import tornado.httpclient as httpclient
import datetime
import time
import sqlite3
sys.path.append("lib")
import base64
import uuid
import urllib
import logging
import logging.config
import redis
import tornado.websocket
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from urllib import unquote
import ConfigParser

logging.config.fileConfig('config/logging.ini')
logger = logging.getLogger('event_server')

cf = ConfigParser.ConfigParser()
cf.read('config/config.ini')
# connect to redis
r = redis.StrictRedis(cf.get("redis","host"), port=cf.get("redis","port"), db=cf.get("redis","db"),password=cf.get("redis","password"))

PORT = cf.get("server","port")


websocket_connected = {}
last_notify_time    = 0

class BaseHandler(tornado.web.RequestHandler):

    def formatReturn(self,code,data=[]):
        rtn = {}
        rtn['code'] = code
        rtn['data'] = data

        self.write(json.dumps(rtn))
        self.finish()

class EmptyHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        logger.debug('receive')
        self.write("ok")
        self.finish()

    def post(self):
        logger.debug('receive post')
        self.write('ok')
        sellf.finish()

# 事件接收处理器
class HappenHandler(tornado.web.RequestHandler):

    def empty_response_handler(self,response):
        logger.info("response:%s"%response.body)
        logger.info("response code:%s"%response.code)
        if response.code == 200:
            r.set(self.eventHandlerResultKey,response.body)
        else :
            r.set(self.eventHandlerResultKey,response.code)
            self.get(self.eventName+'_callError')



    def mail(self,emails,title,content):
        logger.debug("send mail to %s"%emails)
        smtpConfig = {}
        smtpConfig['host'] = cf.get("stmp","host")
        smtpConfig['port'] = cf.get("stmp","port")
        smtpConfig['user'] = cf.get("stmp","user")
        smtpConfig['pass'] = cf.get("stmp","pass")
        smtpConfig['sender'] = cf.get("stmp","sender")
        receivers = emails.split(';')
        try:
            smtp  =  smtplib.SMTP(smtpConfig['host'],smtpConfig['port'])
            smtp.login(smtpConfig['user'],smtpConfig['pass'])
            message = MIMEText(content, 'plain', 'utf-8')
            message['From']    = Header("EventX", 'utf-8')
            message['To']      =  Header(emails, 'utf-8')
            message['Subject'] = Header(title, 'utf-8')
            smtp.sendmail(smtpConfig['sender'],receivers,message.as_string())
            self.write("send email success")
        except Exception, e:
            logger.debug(e)

    @tornado.web.asynchronous
    def post(self):
        self.get()

    @tornado.web.asynchronous
    def get(self,self_event=False):
        app    = self.get_argument('app')
        event  = self.get_argument('event')
        detail = self.request.body
        if self_event:
            app    = 'eventx'
            event  = self_event
            detail = ''
        logger.debug("app:%s event:%s detail:%s"%(app,event,detail))
        self.eventName = "%s_%s"%(app,event)

        nowtime = datetime.datetime.now()
        pipe = r.pipeline()
        pipe.incr('EventX:apps:%s'%app)
        pipe.sadd('EventX:appset',app)
        pipe.incr('EventX:%s:%s'%(app,event))
        # pipe.append('EventX:d:%s:%s'%(app,event),detail)
        pipe.set('EventX:detail:%s:%s'%(app,event),detail)
        basicInfo = {}
        basicInfo['datetime'] = nowtime.strftime('%Y-%m-%d %H:%M:%S')
        pipe.set('EventX:binfo:%s:%s'%(app,event),json.dumps(basicInfo))
        pipe.execute()

        eventHandlerKey = 'EventX:h:%s:%s'%(app,event)
        emailHandlerKey = 'EventX:mail:%s:%s'%(app,event)
        handlerParam = r.get(eventHandlerKey)
        emailParam   = r.get(emailHandlerKey)
        if handlerParam:
            self.eventHandlerResultKey = 'EventX:hr:%s:%s'%(app,event)
            param = json.loads(handlerParam)
            logger.info("trigger %s"%param['url'])
            client = tornado.httpclient.AsyncHTTPClient()

            if param['method']=='GET' and param['url']!='':
                response = client.fetch(param['url'], method='GET', callback = self.empty_response_handler)
            elif param['method']=='POST' and param['url']!='':
                # send_data = {'data':detail}
                response = client.fetch(param['url'], method='POST', body=detail, callback = self.empty_response_handler)
        if emailParam:
            param = json.loads(emailParam)
            logger.debug(param)
            self.mail(param['emails'],param['title'],param['content'])

        # 通知客户的刷新
        for client in websocket_connected:
            logger.debug(client)
            # last_notify_time = time.time()
            websocket_connected[client].notifyClient(app)
        self.write("ok")
        self.finish()

class ApiAppListHandler(BaseHandler):

    def get(self):
        apps = r.smembers('EventX:appset')
        applist = []
        for a in apps:
            applist.append(a)
        self.formatReturn(0,applist)

class ApiSaveHandlerHandler(BaseHandler):

    def post(self):
        app        = self.get_argument('app')
        event      = self.get_argument('event')
        handlerUrl = self.get_argument('handlerUrl')
        method     = self.get_argument('method')
        eventHandlerKey = 'EventX:h:%s:%s'%(app,event)
        handlerParam = {
            'url':handlerUrl,
            'method':method,
        }
        r.set(eventHandlerKey,json.dumps(handlerParam) )
        self.formatReturn(0)

class ApiSaveMailHandler(BaseHandler):

    def post(self):
        app        = self.get_argument('app')
        event      = self.get_argument('event')
        title      = self.get_argument('title')
        content    = self.get_argument('content')
        emails     = self.get_argument('emails')
        eventHandlerKey = 'EventX:mail:%s:%s'%(app,event)
        data = {
            'title':title,
            'content':content,
            'emails':emails,
        }
        r.set(eventHandlerKey,json.dumps(data) )
        self.formatReturn(0)

class ApiEventListHandler(BaseHandler):

    def get(self):
        app    = self.get_argument('app')
        eventKeys = r.keys('EventX:%s:*'%(app))
        # 此处可用pip优化
        data = []
        for k in eventKeys:
            e  = {}
            e['eventName']  = k.split(':')[2]
            e['eventCount'] = r.get(k)
            eventHandlerKey = 'EventX:h:%s:%s'%(app,e['eventName'])
            eventHandlerResultKey = 'EventX:hr:%s:%s'%(app,e['eventName'])
            eventDetailKey = 'EventX:detail:%s:%s'%(app,e['eventName'])
            handlerUrl    = r.get(eventHandlerKey)
            handlerResult = r.get(eventHandlerResultKey)
            detail        = r.get(eventDetailKey)
            basicInfo     = r.get('EventX:binfo:%s:%s'%(app,e['eventName']))
            if handlerUrl:
                e['handlerUrl'] = handlerUrl
            else:
                e['handlerUrl'] = ''
            if handlerResult:
                e['handlerResult'] = handlerResult
            else:
                e['handlerResult'] = ''
            if basicInfo:
                basicInfo = json.loads(basicInfo)
                e['dateTime'] = basicInfo['datetime']
            else:
                e['dateTime'] = ''
            if detail:
                e['detail'] =  unquote(unicode(detail) )
            else:
                e['detail'] =  ''
            data.append(e)
        self.formatReturn(0,data)

class deleteEventHandler(BaseHandler):

    def post(self):
        self.get()

    def get(self):
        app        = self.get_argument('app')
        event      = self.get_argument('event')

        eventHandlerKey = 'EventX:h:%s:%s'%(app,event)
        emailHandlerKey = 'EventX:mail:%s:%s'%(app,event)
        eventCountKey   = 'EventX:%s:%s'%(app,event)
        eventInfoKey    = 'EventX:binfo:%s:%s'%(app,event)
        eventDetailKey  = 'EventX:detail:%s:%s'%(app,event)

        r.delete(eventHandlerKey)
        r.delete(emailHandlerKey)
        r.delete(eventCountKey)
        r.delete(eventInfoKey)
        r.delete(eventDetailKey)

        self.formatReturn(0)



# 通知服务
# 浏览器reg一个随机id,eventx在收到事件请求后遍历所有在线的客户端,并发送消息
class NotifyWebSocketHandler(tornado.websocket.WebSocketHandler):

    def open(self):
        # print("WebSocket opened")
        pass

    def on_message(self, message):
        rec = message.split(':')
        cmd   = rec[0]
        param = rec[1]
        if cmd=='reg':
            self.clientId = param
            websocket_connected[self.clientId] = self
            logger.debug("%s websocket reg"%self.clientId)


    def notifyClient(self,message):
        self.write_message(message)

    def on_close(self):
        del websocket_connected[self.clientId]
        logger.debug("%s websocket closed"%self.clientId)



def make_app():
    return tornado.web.Application([
        (r"/api/appList", ApiAppListHandler),
        (r"/api/eventList", ApiEventListHandler),
        (r"/api/saveHandler", ApiSaveHandlerHandler),
        (r"/api/saveMailInfo", ApiSaveMailHandler),
        (r"/api/deleteEvent", deleteEventHandler),
        (r"/happen", HappenHandler),
        (r"/notify", NotifyWebSocketHandler),
        (r"/", EmptyHandler),
        (r"/(.*\.(htm|html|js|css|woff2|woff|ttf))", tornado.web.StaticFileHandler, {"path": "web"}),
    ],debug=True)

if __name__ == "__main__":
    app = make_app()
    app.listen(PORT)
    logger.info('EventX server start')
    tornado.ioloop.IOLoop.current().start()
