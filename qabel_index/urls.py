from django.conf.urls import include, url
from django.contrib import admin

import django_prometheus.urls

from index_service import views
from index_service import verification


rest_urls = [
    url(r'^$', views.api_root, name='api-root'),
    url(r'^key/$', views.key, name='api-key'),
    url(r'^search/$', views.search, name='api-search'),
    url(r'^update/$', views.update, name='api-update'),
]

verification_urls = [
    url(r'^(?P<id>[^/]+)/(?P<action>confirm|deny)/$', verification.verify, name='verify'),
    url(r'^(?P<id>[^/]+)/$', verification.review, name='review'),
]

urlpatterns = [
    url(r'^admin/', include(admin.site.urls)),
    url(r'^api/v0/', include(rest_urls)),
    url(r'^verify/', include(verification_urls)),
    url('', include(django_prometheus.urls)),
]
