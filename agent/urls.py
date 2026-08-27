"""
agent/urls.py

Day5: Agent app 的路由配置。
"""
from django.urls import path
from agent import views

urlpatterns = [
    path("chat/", views.chat, name="chat"),
]
