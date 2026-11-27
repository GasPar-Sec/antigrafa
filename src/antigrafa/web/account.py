"""The page of the signed-in user (a click on their name): their name, email and password.

Two forms on one page, told apart by the ``form`` field. After a change, the page is
loaded again (``?saved=``) so that reloading it never sends the form twice, scrolled to
the form just saved (``#details``, ``#password``), whose confirmation shows in it: the
user clicked at the bottom of the page and sees it there. A new password signs out the
user's other sessions, not this one.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import password_validators_help_texts
from django.http import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

# Shown once a form is saved (messages, not secrets)
SAVED = {"profile": "Your details are saved.", "password": "Your password is changed."}  # nosec B105


class ProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "email"]
        labels = {"first_name": "First name", "last_name": "Last name", "email": "Email"}


@login_required
@require_http_methods(["GET", "POST"])
def account(request: HttpRequest):
    user = request.user
    profile = ProfileForm(instance=user)
    password = PasswordChangeForm(user)
    if request.method == "POST":
        which = request.POST.get("form")
        if which == "profile":
            profile = ProfileForm(request.POST, instance=user)
            if profile.is_valid():
                profile.save()
                return redirect(reverse("account") + "?saved=profile#details")
        elif which == "password":
            password = PasswordChangeForm(user, request.POST)
            if password.is_valid():
                password.save()
                update_session_auth_hash(request, password.user)  # this session stays signed in
                return redirect(reverse("account") + "?saved=password#password")
    return render(
        request,
        "antigrafa/account.html",
        {
            "page": "account",
            "profile": profile,
            "password": password,
            "password_rules": password_validators_help_texts(),
            "saved": request.GET.get("saved", "") if request.GET.get("saved", "") in SAVED else "",
            "saved_message": SAVED.get(request.GET.get("saved", ""), ""),
        },
    )
