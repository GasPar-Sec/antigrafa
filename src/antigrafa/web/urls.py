from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("backup/", views.backup_page, name="backup_page"),
    path("restore/", views.restore_page, name="restore_page"),
    path("login/", auth_views.LoginView.as_view(template_name="antigrafa/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/status", views.status, name="status"),
    path("api/overview", views.overview, name="overview"),
    path("api/running", views.running, name="running"),
    path("api/guests", views.guests, name="guests"),
    path("api/backups", views.backups, name="start"),  # GET: list, POST: start a backup
    path("api/backups/<int:run_id>/cancel", views.cancel, name="cancel"),  # any run: backup or restore
    path("api/restores", views.restore, name="restore"),
    path("api/restore-locations", views.restore_locations, name="restore_locations"),
    path("api/jobs", views.jobs, name="jobs"),
    path("api/jobs/<str:name>/run", views.run_job, name="run_job"),
]
