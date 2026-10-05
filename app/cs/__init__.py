# -*- coding: utf-8 -*-
"""人工客服（Customer Service）子系统。

- models: 坐席、登录会话、客服会话、消息、参与人
- service: 队列/接单/转接/邀请/关闭状态机与事件广播
- relay: 坐席回复回投到平台私信/评论的适配层
- api: 访客与坐席 HTTP/SSE 接口
"""
