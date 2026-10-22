from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import status
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ConsultationSession, User, Wallet
from .permissions import HasRechargeWebhookSecret, IsCustomer
from .serializers import (
    RechargeCallbackSerializer,
    RechargeInitiationSerializer,
    RegistrationSerializer,
    EmptyActionSerializer,
    SessionAssignmentSerializer,
    SessionDetailSerializer,
)
from .services import RechargeError, finalize_recharge, initiate_recharge
from .session_services import (
    SessionError, start_session, accept_session, assign_provider,
    bill_session, create_session, end_session,
)


class APIErrorMixin:
    def handle_exception(self, exc):
        if isinstance(exc, (RechargeError, SessionError)):
            response = Response(
                {'detail': exc.messages, 'code': exc.code},
                status={'forbidden': 403, 'not_found': 404}.get(exc.code, 400),
            )
        elif isinstance(exc, DjangoValidationError):
            response = Response(
                exc.message_dict if hasattr(exc, 'message_dict') else {'detail': exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            response = super().handle_exception(exc)
        response.data = {'errors': response.data}
        return response


class RegistrationView(APIErrorMixin, APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = serializer.save()
        return Response(
            {'id': customer.pk, 'username': customer.username, 'role': customer.role},
            status=status.HTTP_201_CREATED,
        )


class TokenView(APIErrorMixin, ObtainAuthToken):
    authentication_classes = []
    permission_classes = [AllowAny]


class WalletView(APIErrorMixin, APIView):
    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request):
        try:
            wallet = Wallet.objects.get(user=request.user)
        except Wallet.DoesNotExist:
            raise RechargeError('Customer has no wallet.', code='missing_wallet') from None
        return Response({'balance': format(wallet.balance, '.2f')})


class RechargeInitiationView(APIErrorMixin, APIView):
    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request):
        serializer = RechargeInitiationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recharge = initiate_recharge(request.user, **serializer.validated_data)
        return Response(
            {'reference': str(recharge.reference), 'status': recharge.status},
            status=status.HTTP_201_CREATED,
        )


class RechargeCallbackView(APIErrorMixin, APIView):
    authentication_classes = []
    permission_classes = [HasRechargeWebhookSecret]

    def post(self, request):
        serializer = RechargeCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recharge = finalize_recharge(**serializer.validated_data)
        if recharge.status != serializer.validated_data['outcome']:
            return Response(
                {'errors': {'detail': ['Recharge already finalized with a different outcome.'],
                            'code': 'conflicting_outcome'}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({'reference': str(recharge.reference), 'status': recharge.status})


class SessionCreateView(APIErrorMixin, APIView):
    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request):
        serializer = EmptyActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = create_session(request.user)
        return Response(SessionDetailSerializer(session).data, status=status.HTTP_201_CREATED)


class ParticipantSessionView(APIErrorMixin, APIView):
    def get_session(self, session_id):
        try:
            if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 19
                    or not session_id.isascii() or not session_id.isdecimal()
                    or session_id.startswith('0')):
                raise ValueError
            session_id = int(session_id)
            if not 0 < session_id <= 9223372036854775807:
                raise ValueError
        except (ValueError, TypeError):
            raise SessionError('Session not found.', code='not_found') from None
        try:
            scope = (Q(customer=self.request.user) if self.request.user.role == User.Role.USER
                     else Q(provider__user=self.request.user))
            return ConsultationSession.objects.select_related('provider').filter(
                scope
            ).get(pk=session_id)
        except ConsultationSession.DoesNotExist:
            raise SessionError('Session not found.', code='not_found') from None


class SessionDetailView(ParticipantSessionView):
    def get(self, request, session_id):
        return Response(SessionDetailSerializer(self.get_session(session_id)).data)


class SessionActionView(ParticipantSessionView):
    action = None

    def post(self, request, session_id):
        session = self.get_session(session_id)
        required_role = {
            'assign': User.Role.USER, 'start': User.Role.USER,
            'accept': User.Role.PROVIDER,
        }.get(self.action)
        if required_role and request.user.role != required_role:
            raise SessionError('Account role is not permitted.', code='forbidden')
        serializer_class = SessionAssignmentSerializer if self.action == 'assign' else EmptyActionSerializer
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        if self.action == 'assign':
            result = assign_provider(session.pk, request.user, **serializer.validated_data)
        elif self.action == 'accept':
            result = accept_session(session.pk, request.user)
        elif self.action == 'start':
            result = start_session(session.pk, request.user)
        elif self.action == 'bill':
            result = bill_session(session.pk)
        else:
            result = end_session(session.pk, request.user)
        return Response(SessionDetailSerializer(result).data)
