from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class UploadedFile(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="uploads")
    file = models.FileField(upload_to="uploads/")
    filename = models.CharField(max_length=512)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # stores basic cached EDA json (optional)
    eda_summary = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.filename} ({self.owner})"
