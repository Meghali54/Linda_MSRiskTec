from django.contrib import admin
from .models import UploadedFile

@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ("filename", "owner", "uploaded_at")
    search_fields = ("filename", "owner__username")
