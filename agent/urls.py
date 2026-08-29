"""
agent/urls.py

Day5: Agent app 的路由配置。
Day7: 新增联系表单路由 + 对话历史路由
Day8: 新增产品列表 / 订单 API 路由（前端下单页用）
Day9: 新增留言/定制详情与标记已读 + 订单状态变更端点（库存联动）
"""
from django.urls import path
from agent import views

urlpatterns = [
    path("chat/", views.chat, name="chat"),
    path("contact/", views.contact_submit, name="contact_submit"),
    path("custom/", views.custom_submit, name="custom_submit"),
    path("history/<str:session_id>/", views.history, name="history"),
    # 留言 / 定制管理（P0 补齐：标记已读；P1 补齐：列表/删除）
    path("contact-msgs/", views.contact_messages_list, name="contact_messages_list"),
    path("custom-reqs/", views.custom_requests_list, name="custom_requests_list"),
    path("contact-msg/<int:message_id>/", views.contact_message_api, name="contact_message_api"),
    path("custom-req/<int:request_id>/", views.custom_request_api, name="custom_request_api"),
    # 下单页相关
    path("products/", views.product_list, name="product_list"),
    path("orders/", views.create_order_api, name="create_order_api"),
    path("orders/<str:order_no>/", views.order_detail, name="order_detail"),
]
