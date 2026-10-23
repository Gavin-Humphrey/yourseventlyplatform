from django.urls import path

from . import views

app_name = "marketing_studio"

urlpatterns = [
    path(
        "",
        views.ai_marketing_studio,
        name="studio",
    ),
]