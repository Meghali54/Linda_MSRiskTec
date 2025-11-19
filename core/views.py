# ...existing code...
import os
import io
import json
import csv
import logging
import importlib
from functools import lru_cache

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.conf import settings
from django.http import JsonResponse, HttpResponse

from .forms import UploadForm
from .models import UploadedFile
from django.contrib.auth.forms import UserCreationForm

logger = logging.getLogger(__name__)

# -------------------------
# Lazy import helpers
# -------------------------
@lru_cache(maxsize=None)
def _lazy(name):
    try:
        return importlib.import_module(name)
    except Exception as exc:
        logger.debug("Optional module %s not available: %s", name, exc)
        return None

def get_pandas():
    return _lazy("pandas")

def get_numpy():
    return _lazy("numpy")

def get_matplotlib():
    mpl = _lazy("matplotlib")
    if mpl is not None:
        try:
            mpl.use("Agg")
        except Exception:
            logger.debug("Could not set matplotlib backend to Agg")
    return mpl

def get_pyplot():
    mpl = get_matplotlib()
    if mpl is None:
        return None
    return _lazy("matplotlib.pyplot")

def get_pdf_extract():
    pdfminer = _lazy("pdfminer.high_level")
    if pdfminer is not None:
        return getattr(pdfminer, "extract_text", None)
    pypdf2 = _lazy("PyPDF2")
    if pypdf2 is not None:
        def _extract(path, **kwargs):
            try:
                reader = pypdf2.PdfReader(path)
                pages = [p.extract_text() or "" for p in reader.pages]
                return "\n".join(pages)
            except Exception:
                logger.exception("PyPDF2 extraction failed for %s", path)
                return ""
        return _extract
    return None

def get_textract():
    return _lazy("textract")

def get_ProfileReport():
    mod = _lazy("ydata_profiling")
    return getattr(mod, "ProfileReport", None) if mod is not None else None

def get_genai():
    return _lazy("google.generativeai") or _lazy("google.genai") or _lazy("genai")

# -------------------------
# Utilities
# -------------------------
def convert_json(o):
    np = get_numpy()
    pd = get_pandas()
    try:
        if np is not None:
            if isinstance(o, (np.integer,)):
                return int(o)
            if isinstance(o, (np.floating,)):
                return float(o)
            if isinstance(o, (np.ndarray,)):
                return o.tolist()
    except Exception:
        pass
    if pd is not None:
        try:
            if pd.isna(o):
                return None
        except Exception:
            pass
    try:
        return int(o)
    except Exception:
        try:
            return float(o)
        except Exception:
            return str(o)

# -------------------------
# Views
# -------------------------
def home_view(request):
    files = None
    if request.user.is_authenticated:
        files = request.user.uploads.all().order_by("-uploaded_at")
    return render(request, "core/home.html", {"files": files})

def register_view(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Account created successfully. You can now log in.")
            return redirect("core:login")
        else:
            messages.error(request, "Please correct the highlighted fields.")
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
            messages.success(request, "File uploaded.")
            # run EDA in-process if small; recommended: enqueue for background in production
            try:
                eda = generate_eda(obj, request)
                obj.eda_summary = eda
                obj.save()
            except Exception as e:
                logger.exception("EDA generation failed: %s", e)
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
    charts_dir = os.path.join(settings.MEDIA_ROOT, "charts")
    if os.path.isdir(charts_dir):
        for fname in os.listdir(charts_dir):
            if fname.startswith(f"file_{obj.id}_"):
                charts.append(os.path.join(settings.MEDIA_URL, "charts", fname))
    return render(request, "core/file_detail.html", {"file": obj, "eda": eda, "charts": charts})

@login_required
def ask_file_view(request, pk):
    obj = get_object_or_404(UploadedFile, pk=pk, owner=request.user)
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=400)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    question = data.get("question", "").strip()
    if not question:
        return JsonResponse({"error": "Empty question"}, status=400)

    ext = obj.filename.lower().split(".")[-1]
    file_text = ""

    try:
        pd = get_pandas()
        if ext in ("csv", "txt"):
            if pd is not None:
                df = pd.read_csv(obj.file.path, nrows=200)
                file_text = df.to_csv(index=False)
            else:
                with open(obj.file.path, "r", encoding="utf-8", errors="ignore") as fh:
                    reader = csv.reader(fh)
                    rows = []
                    for i, row in enumerate(reader):
                        rows.append(",".join(row))
                        if i >= 200:
                            break
                    file_text = "\n".join(rows)

        elif ext in ("xls", "xlsx"):
            if pd is not None:
                df = pd.read_excel(obj.file.path, nrows=200)
                file_text = df.to_csv(index=False)
            else:
                file_text = "Excel preview requires pandas on server."

        elif ext == "pdf":
            extractor = get_pdf_extract()
            if extractor is not None:
                extracted = extractor(obj.file.path)
                file_text = extracted[:20000] if extracted else "No readable text found in PDF."
            else:
                file_text = "PDF extraction library not available on server."
        else:
            file_text = "Unsupported file type."
    except Exception as e:
        logger.exception("Error reading file preview: %s", e)
        file_text = f"Error reading file: {e}"

    prompt = f"""You are a smart assistant. The user uploaded a file.
Extracted File Content:
{file_text}

USER QUESTION:
{question}

Answer concisely. When referencing data, use exact column names.
"""

    answer = None
    try:
        genai = get_genai()
        if genai is None or not settings.GEMINI_API_KEY:
            answer = "AI not configured on server."
        else:
            try:
                # Best-effort generic handling — adapt to the SDK you use
                if hasattr(genai, "configure"):
                    genai.configure(api_key=settings.GEMINI_API_KEY)
                if hasattr(genai, "GenerativeModel"):
                    Model = getattr(genai, "GenerativeModel")
                    model = Model("gemini-2.0-flash")
                    resp = model.generate_content(prompt)
                    answer = getattr(resp, "text", None) or "No response produced."
                else:
                    answer = "GenAI SDK present but SDK shape unsupported by this code."
            except Exception as e:
                logger.exception("GenAI call failed: %s", e)
                answer = f"AI error: {e}"
    except Exception as e:
        logger.debug("GenAI integration skipped: %s", e)
        answer = f"AI error: {e}"

    return JsonResponse({"answer": answer})

# -------------------------
# EDA generation (lazy heavy libs)
# -------------------------
def generate_eda(uploaded_obj, request=None):
    pd = get_pandas()
    np = get_numpy()
    pyplot = get_pyplot()
    ProfileReport = get_ProfileReport()
    pdf_extract = get_pdf_extract()

    file_path = uploaded_obj.file.path
    ext = uploaded_obj.filename.lower().split(".")[-1]

    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_ROOT, "charts"), exist_ok=True)

    df = None

    # PDF
    if ext == "pdf":
        try:
            if pdf_extract is not None:
                text = pdf_extract(file_path)
                df = pd.DataFrame({"document_content": [text]}) if pd is not None else None
            else:
                # fallback: try textract if available
                tex = get_textract()
                if tex is not None:
                    text = tex.process(file_path).decode("utf-8", errors="ignore")
                    df = pd.DataFrame({"document_content": [text]}) if pd is not None else None
                else:
                    return {"error": "No PDF extraction library available"}
        except Exception as e:
            logger.exception("PDF extraction error: %s", e)
            return {"error": f"PDF extraction error: {e}"}

    # CSV/TXT
    elif ext in ("csv", "txt"):
        try:
            if pd is not None:
                df = pd.read_csv(file_path)
            else:
                # minimal CSV parser fallback
                rows = []
                with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    rdr = csv.reader(fh)
                    for row in rdr:
                        rows.append(row)
                if rows:
                    headers = rows[0]
                    data = rows[1:]
                    if data:
                        df = _rows_to_minidf(headers, data)
                    else:
                        df = _rows_to_minidf(headers, [])
                else:
                    df = None
        except Exception as e:
            logger.exception("CSV read error: %s", e)
            return {"error": f"CSV/TXT read error: {e}"}

    # Excel
    elif ext in ("xls", "xlsx"):
        try:
            if pd is not None:
                df = pd.read_excel(file_path)
            else:
                return {"error": "Excel reading requires pandas"}
        except Exception as e:
            logger.exception("Excel read error: %s", e)
            return {"error": f"Excel read error: {e}"}
    else:
        return {"error": "Unsupported file format for EDA"}

    if df is None:
        return {"error": "Could not construct DataFrame"}

    # Profiling (optional)
    report_path = None
    if ProfileReport is not None:
        try:
            profile = ProfileReport(df, title=f"{uploaded_obj.filename} Profiling Report", explorative=True)
            report_path = os.path.join(settings.REPORTS_DIR, f"report_{uploaded_obj.id}.html")
            profile.to_file(report_path)
        except Exception as e:
            logger.exception("Profiling error: %s", e)
            report_path = None

    # Charts (first up to 4 numeric)
    charts_urls = []
    try:
        numeric_cols = []
        if pd is not None:
            numeric_cols = df.select_dtypes(include="number").columns[:4]
        else:
            # fallback: no numeric detection without pandas; skip charts
            numeric_cols = []
        for i, col in enumerate(numeric_cols):
            try:
                if pyplot is None:
                    break
                pyplot.figure(figsize=(6, 4))
                arr = df[col].dropna().values if pd is not None else []
                pyplot.hist(arr, bins=30)
                pyplot.title(f"{col} distribution")
                fname = f"file_{uploaded_obj.id}_hist_{i}.png"
                fpath = os.path.join(settings.MEDIA_ROOT, "charts", fname)
                pyplot.savefig(fpath)
                pyplot.close()
                charts_urls.append(os.path.join(settings.MEDIA_URL, "charts", fname))
            except Exception:
                logger.exception("Chart generation failed for %s", col)
                try:
                    pyplot.close()
                except Exception:
                    pass
    except Exception:
        pass

    # Basic EDA summary
    try:
        if pd is not None:
            basic_eda = {
                "dtypes": df.dtypes.astype(str).to_dict(),
                "missing": df.isnull().sum().to_dict(),
                "describe": df.describe(include="all").fillna("").to_dict()
            }
            try:
                first_col = next(iter(basic_eda["describe"].values()))
                basic_eda["describe_headers"] = list(first_col.keys())
            except Exception:
                basic_eda["describe_headers"] = []
        else:
            basic_eda = {"note": "pandas not installed; limited EDA"}
    except Exception as e:
        logger.exception("Basic EDA generation error: %s", e)
        basic_eda = {"error": f"Basic EDA generation error: {e}"}

    summary = {
        "shape": getattr(df, "shape", None),
        "columns": list(df.columns.astype(str)) if hasattr(df, "columns") else None,
        "head": (df.head(5).to_dict() if hasattr(df, "head") else None),
        "basic_eda": basic_eda,
        "charts": charts_urls,
        "report_url": (os.path.join(settings.MEDIA_URL, f"reports/report_{uploaded_obj.id}.html") if report_path else None)
    }

    # JSON-safe conversion
    summary = json.loads(json.dumps(summary, default=convert_json))
    return summary

# small helper to build a minimal dataframe-like dict when pandas missing
def _rows_to_minidf(headers, rows):
    data = {h: [] for h in headers}
    for r in rows:
        for i, h in enumerate(headers):
            data[h].append(r[i] if i < len(r) else "")
    class MiniDF:
        def __init__(self, data):
            self._data = data
            self.columns = list(data.keys())
        def head(self, n=5):
            head_rows = []
            for i in range(min(n, len(next(iter(self._data.values()), [])))):
                head_rows.append({k: self._data[k][i] for k in self.columns})
            return head_rows
        def to_dict(self):
            return self._data
        def __getattr__(self, item):
            raise AttributeError
    return MiniDF(data)

@user_passes_test(lambda u: u.is_staff)
def admin_dashboard_view(request):
    User = __import__("django.contrib.auth").contrib.auth.get_user_model()
    total_users = User.objects.count()
    total_uploads = UploadedFile.objects.count()
    recent = UploadedFile.objects.order_by("-uploaded_at")[:10]
    return render(request, "core/admin_dashboard.html", {"total_users": total_users, "total_uploads": total_uploads, "recent": recent})
# ...existing code...