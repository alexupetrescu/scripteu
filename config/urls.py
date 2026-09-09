from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # Django's own login/logout views. LoginRequiredMiddleware exempts the
    # login page itself; everything else in this project needs a session.
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('esc.urls')),
]
