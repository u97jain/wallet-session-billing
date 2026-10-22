from django.urls import path

from .views import (
    RechargeCallbackView,
    RechargeInitiationView,
    RegistrationView,
    TokenView,
    WalletView,
    SessionCreateView,
    SessionDetailView,
    SessionActionView,
)

urlpatterns = [
    path('register/', RegistrationView.as_view(), name='register'),
    path('token/', TokenView.as_view(), name='token'),
    path('wallet/', WalletView.as_view(), name='wallet'),
    path('recharges/', RechargeInitiationView.as_view(), name='recharge-initiate'),
    path('recharges/callback/', RechargeCallbackView.as_view(), name='recharge-callback'),
    path('sessions/', SessionCreateView.as_view(), name='session-create'),
    path('sessions/<str:session_id>/', SessionDetailView.as_view(), name='session-detail'),
    path('sessions/<str:session_id>/assign/', SessionActionView.as_view(action='assign'), name='session-assign'),
    path('sessions/<str:session_id>/accept/', SessionActionView.as_view(action='accept'), name='session-accept'),
    path('sessions/<str:session_id>/start/', SessionActionView.as_view(action='start'), name='session-start'),
    path('sessions/<str:session_id>/bill/', SessionActionView.as_view(action='bill'), name='session-bill'),
    path('sessions/<str:session_id>/end/', SessionActionView.as_view(action='end'), name='session-end'),
]
