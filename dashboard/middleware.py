from django.contrib.auth.models import AnonymousUser
from django.urls import resolve
from bookings.models import AdminProfile

class PathTokenAuthenticationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response


    def __call__(self, request):
        try:
            match = resolve(request.path_info)

            token = match.kwargs.get("user_token")

            if token:
                profile = AdminProfile.objects.select_related('user').get(access_token=token)

                request.user = profile.user
            else:
                request.user = AnonymousUser()

        except Exception:
            request.user = AnonymousUser()

        return self.get_response(request)