from rest_framework import generics
from bookings.serializers import MasterSerializer
from bookings.models import Master
from rest_framework.views import APIView
from rest_framework.response import Response

class DashboardListView(APIView):
    def get(self, request):
        return Response({"status": "API is working"})


class TeamListView(generics.ListAPIView):
    serializer_class = MasterSerializer

