"""Маршруты приложения assistant."""

from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("", views.chat, name="chat"),
    path("api/chat/", views.api_chat, name="api_chat"),
    path("api/clear/", views.api_clear, name="api_clear"),
    path("api/export/", views.api_export, name="api_export"),
]