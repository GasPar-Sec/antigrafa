from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", auth_views.LoginView.as_view(template_name="antigrafa/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/status", views.status, name="status"),
    path("api/guests", views.guests, name="guests"),
    path("api/backups", views.start, name="start"),
    path("api/backups/<int:run_id>/cancel", views.cancel, name="cancel"),
]
