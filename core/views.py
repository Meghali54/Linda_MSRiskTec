import os
import io
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from .forms import UploadForm, RegisterForm
from .models import UploadedFile
from django.contrib import messages
from django.shortcuts import redirect, render
from django.contrib.auth.forms import UserCreationForm

# data libs
import pandas as pd
import matplotlib.pyplot as plt

# For PDF text extraction
try:
    import textract
except Exception:
    textract = None

# For Gemini / Google Gen AI
# Note: adapt the exact import to the SDK you choose. Example below is illustrative.
# Install and follow official Gen AI / Gemini docs (library and method names may change).
try:
    from google import genai
except Exception:
    genai = None

def home_view(request):
    files = None
    if request.user.is_authenticated:
        files = request.user.uploads.all().order_by("-uploaded_at")
    return render(request, "core/home.html", {"files": files})


from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.shortcuts import redirect, render

def register_view(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "🎉 Account created successfully! You can now log in.")
            return redirect("core:login")
        else:
            messages.error(request, "There was an error. Please correct the highlighted fields.")
    else:
        form = UserCreationForm()

    return render(request, "core/register.html", {"form": form})


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("core:home")
        messages.error(request, "Invalid credentials.")
    return render(request, "core/login.html")

def logout_view(request):
    logout(request)
    return redirect("core:home")

@login_required
def upload_view(request):
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            obj = UploadedFile.objects.create(owner=request.user, file=f, filename=f.name)
            messages.success(request, "File uploaded. Generating EDA...")
            # optional: run EDA immediately and save summary
            try:
                eda = generate_eda(obj, request)
                obj.eda_summary = eda
                obj.save()
            except Exception as e:
                messages.warning(request, f"EDA generation failed: {e}")
            return redirect("core:file_detail", pk=obj.pk)
    else:
        form = UploadForm()
    return render(request, "core/upload.html", {"form": form})

@login_required
def file_detail_view(request, pk):
    obj = get_object_or_404(UploadedFile, pk=pk, owner=request.user)
    eda = obj.eda_summary or {}
    charts = []
    # look for generated charts in media/charts/<obj.id>_*.png
    charts_dir = os.path.join(settings.MEDIA_ROOT, "charts")
    if os.path.isdir(charts_dir):
        for fname in os.listdir(charts_dir):
            if fname.startswith(f"file_{obj.id}_"):
                charts.append(settings.MEDIA_URL + "charts/" + fname)
    return render(request, "core/file_detail.html", {"file": obj, "eda": eda, "charts": charts})

@login_required
def ask_file_view(request, pk):
    """
    AJAX endpoint: User asks a question about an uploaded file.
    Returns JSON: {"answer": "..."}
    """
    obj = get_object_or_404(UploadedFile, pk=pk, owner=request.user)

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=400)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    question = data.get("question", "").strip()
    if not question:
        return JsonResponse({"error": "Empty question"}, status=400)

    # ----------------------------------------------------------------------
    # Extract preview text from uploaded file
    # ----------------------------------------------------------------------
    ext = obj.filename.lower().split(".")[-1]
    file_text = ""

    try:
        if ext in ("csv", "txt"):
            df = pd.read_csv(obj.file.path, nrows=200)
            file_text = df.to_csv(index=False)

        elif ext in ("xls", "xlsx"):
            df = pd.read_excel(obj.file.path, nrows=200)
            file_text = df.to_csv(index=False)

        elif ext == "pdf":
            try:
                from pdfminer.high_level import extract_text
                extracted = extract_text(obj.file.path)
                file_text = extracted[:20000] if extracted else "No readable text found in PDF."
            except Exception as e:
                file_text = f"PDF extraction failed: {str(e)}"
        else:
            file_text = "Unsupported file type."

    except Exception as e:
        file_text = f"Error reading file: {e}"

    # ----------------------------------------------------------------------
    # Build prompt
    # ----------------------------------------------------------------------
    prompt = f"""
You are a smart assistant. The user uploaded a file. 
Below is extracted text from a PDF (may be partial). 
Even if the text is incomplete or messy, ALWAYS answer based on whatever text is available.

Extracted File Content:
{file_text}

USER QUESTION:
-----------------------
{question}

Answer concisely. When referencing data, use exact column names.
"""

    # ----------------------------------------------------------------------
    # Gemini AI Call
    # ----------------------------------------------------------------------
    try:
        import google.generativeai as genai

        if not settings.GEMINI_API_KEY:
            return JsonResponse({"answer": "Gemini API key is missing on server."})

        genai.configure(api_key=settings.GEMINI_API_KEY)

        model = genai.GenerativeModel("gemini-2.0-flash")

        response = model.generate_content(prompt)

        # Extract text safely
        answer = getattr(response, "text", None) or "No response produced."

    except Exception as e:
        answer = f"AI error: {e}"

    return JsonResponse({"answer": answer})

import os
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from django.conf import settings
from pdfminer.high_level import extract_text
from ydata_profiling import ProfileReport


def convert_json(o):
    if isinstance(o, (np.integer, np.int32, np.int64)):
        return int(o)
    if isinstance(o, (np.floating, np.float32, np.float64)):
        return float(o)
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    if pd.isna(o):
        return None
    return str(o)


def generate_eda(uploaded_obj, request=None):
    """
    Enhanced EDA using:
    - ydata_profiling for full HTML report
    - pdfminer.six for PDF text extraction
    - Matplotlib numeric distribution charts
    - Basic EDA summary (dtypes, missing values, describe stats)
    """

    file_path = uploaded_obj.file.path
    ext = uploaded_obj.filename.lower().split(".")[-1]

    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_ROOT, "charts"), exist_ok=True)

    # ------------------------------------------------------------------
    # 1️⃣ HANDLE PDF → EXTRACT TEXT → CREATE DATAFRAME
    # ------------------------------------------------------------------
    if ext == "pdf":
        try:
            text = extract_text(file_path)
            df = pd.DataFrame({"document_content": [text]})
        except Exception as e:
            return {"error": f"PDF extraction error: {str(e)}"}

    # ------------------------------------------------------------------
    # 2️⃣ HANDLE CSV / TXT
    # ------------------------------------------------------------------
    elif ext in ("csv", "txt"):
        try:
            df = pd.read_csv(file_path)
        except Exception as e:
            return {"error": f"CSV/TXT read error: {str(e)}"}

    # ------------------------------------------------------------------
    # 3️⃣ HANDLE EXCEL
    # ------------------------------------------------------------------
    elif ext in ("xls", "xlsx"):
        try:
            df = pd.read_excel(file_path)
        except Exception as e:
            return {"error": f"Excel read error: {str(e)}"}

    else:
        return {"error": "Unsupported file format for EDA"}

    # ------------------------------------------------------------------
    # 4️⃣ GENERATE YDATA PROFILING REPORT (HTML)
    # ------------------------------------------------------------------
    try:
        profile = ProfileReport(df, title=f"{uploaded_obj.filename} Profiling Report", explorative=True)
        report_path = os.path.join(settings.REPORTS_DIR, f"report_{uploaded_obj.id}.html")
        profile.to_file(report_path)
    except Exception as e:
        print("Profiling error:", e)
        report_path = None

    # ------------------------------------------------------------------
    # 5️⃣ SAVE SIMPLE CHARTS (FIRST 4 NUMERIC COLUMNS)
    # ------------------------------------------------------------------
    charts_urls = []
    numeric_cols = df.select_dtypes(include="number").columns[:4]

    for i, col in enumerate(numeric_cols):
        try:
            plt.figure(figsize=(6, 4))
            df[col].dropna().hist()
            plt.title(f"{col} distribution")
            fname = f"file_{uploaded_obj.id}_hist_{i}.png"
            fpath = os.path.join(settings.MEDIA_ROOT, "charts", fname)
            plt.savefig(fpath)
            plt.close()
            charts_urls.append(settings.MEDIA_URL + "charts/" + fname)
        except:
            pass

    # ------------------------------------------------------------------
    # 6️⃣ BASIC EDA SUMMARY (NEW)
    # ------------------------------------------------------------------
    try:
        basic_eda = {
            "dtypes": df.dtypes.astype(str).to_dict(),
            "missing": df.isnull().sum().to_dict(),
            "describe": df.describe(include="all").fillna("").to_dict()
        }
        try:
            first_col = next(iter(basic_eda["describe"].values()))
            basic_eda["describe_headers"] = list(first_col.keys())
        except:
            basic_eda["describe_headers"] = []
    except Exception as e:
        basic_eda = {"error": f"Basic EDA generation error: {e}"}

    # ------------------------------------------------------------------
    # 7️⃣ FINAL JSON-SAFE SUMMARY FOR UI
    # ------------------------------------------------------------------
    summary = {
        "shape": df.shape,
        "columns": list(df.columns.astype(str)),
        "head": df.head(5).to_dict(),
        "basic_eda": basic_eda,
        "charts": charts_urls,
        "report_url": settings.MEDIA_URL + f"reports/report_{uploaded_obj.id}.html"
                        if report_path else None
    }

    summary = json.loads(json.dumps(summary, default=convert_json))
    return summary

@user_passes_test(lambda u: u.is_staff)
def admin_dashboard_view(request):
    total_users = __import__("django.contrib.auth").contrib.auth.get_user_model().objects.count()
    total_uploads = UploadedFile.objects.count()
    recent = UploadedFile.objects.order_by("-uploaded_at")[:10]
    return render(request, "core/admin_dashboard.html", {"total_users": total_users, "total_uploads": total_uploads, "recent": recent})
