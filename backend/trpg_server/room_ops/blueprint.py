"""rooms 蓝图对象：多个路由模块共享同一个蓝图，保持 endpoint 前缀 ``rooms.*`` 不变。"""
from flask import Blueprint

bp = Blueprint("rooms", __name__)
