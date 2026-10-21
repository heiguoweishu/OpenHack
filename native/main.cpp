/* GlassHack 原生外壳 —— 真正的 Win32 窗口 + 系统级毛玻璃 + 透明 WebView2。

职责边界很明确：这个 exe 只管"窗口"，不管业务。
  1. 把 GlassHackCore.exe（原 PyInstaller 那套 Python 后端）拉起来
  2. 读它写的 server.ready 拿到端口
  3. 建**自己的**无边框 Win32 窗口，按 ExplorerBlurMica 的做法上系统级毛玻璃
  4. 托管 WebView2（背景透明），让它承载现有的 HTML/CSS 界面
  5. 后端进程一退出，窗口跟着关

为什么这才叫"原生液态玻璃"：
  毛玻璃是 **DWM 合成器实时**从窗口背后采样并模糊出来的，由操作系统绘制；
  窗口移动、壁纸变化都会跟着变。以前是让 Chromium 在页面里 paint 一张背景图
  或用 CSS 画渐变 —— 那是"画上去的"，不是系统给的材质。

刻意不做的事：**不加 WS_EX_LAYERED**。
  给 Edge 的 --app 窗口加分层属性会让窗口直接消失；
  ExplorerBlurMica 这类做法也不依赖分层窗口，只调 DWM/accent 接口。
*/
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <windowsx.h>      // GET_X_LPARAM / GET_Y_LPARAM 在这里
#include <dwmapi.h>
#include <shlwapi.h>
#include <objbase.h>
#include <tlhelp32.h>      // 枚举进程：启动前清掉占着 bin 的旧后端

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <thread>

#include "WebView2.h"

// ============================================================ 常量

static const wchar_t* kCoreExe = L"OpenHackCore.exe";
static const int kBorderHit = 6;              // 判定为缩放边框的像素宽度

// DWMWA_SYSTEMBACKDROP_TYPE（Win11 22H2+ 官方接口）
static const DWORD kDwmwaSystemBackdrop = 1029;
static const DWORD kDwmsbtTransient = 3;      // 3 = Acrylic

// SetWindowCompositionAttribute 的 accent（Win10 用）
static const DWORD kWcaAccentPolicy = 19;
enum AccentState {
    kAccentAcrylic = 4,        // ACCENT_ENABLE_ACRYLICBLURBEHIND
    kAccentBlurBehind = 3,     // 老版霜玻璃，兜底
};

struct ACCENT_POLICY {
    DWORD AccentState;
    DWORD AccentFlags;
    DWORD GradientColor;       // 0xAABBGGRR
    DWORD AnimationId;
};
struct WINCOMPATTRDATA {
    DWORD Attribute;
    PVOID Data;
    SIZE_T SizeOfData;
};
using FnSetWindowCompositionAttribute = BOOL(WINAPI*)(HWND, WINCOMPATTRDATA*);
using FnDwmSetWindowAttribute = HRESULT(WINAPI*)(HWND, DWORD, LPCVOID, DWORD);

// ============================================================ 全局

static HWND g_hwnd = nullptr;
static PROCESS_INFORMATION g_child{};
static bool g_hasChild = false;
static ICoreWebView2Controller* g_controller = nullptr;   // WM_SIZE 时要重设它的区域
static int g_port = 8733;

// ============================================================ 小工具

// 无控制台、也没带调试器的情况下，只能靠日志文件定位崩溃发生在哪一步
static std::wstring LogPath() {
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    std::wstring d = std::wstring(appdata) + L"\\OpenHack";
    CreateDirectoryW(d.c_str(), nullptr);
    // 文件名带 pid：多实例、或被杀毒软件短暂占用时不会互相顶掉；
    // 之前固定叫 native.log，被占用后整个日志就写不进去，排查时一片空白。
    wchar_t name[64];
    swprintf(name, 64, L"\\native_%lu.log", GetCurrentProcessId());
    return d + name;
}

static void Log(const wchar_t* msg) {
    FILE* f = _wfopen(LogPath().c_str(), L"ab");
    if (f) {
        fwprintf(f, L"[%lu] %ls\n", GetCurrentProcessId(), msg);
        fclose(f);
    }
    OutputDebugStringW(msg);
    OutputDebugStringW(L"\n");
}

// 页面真的跑起来了就写这个文件。后端（GlassHackCore.exe）会盯着：
// 如果本进程在页面 ready 之前就死了（WebView2 渲染进程起不来会直接崩），
// 它就自作主张用系统浏览器把界面打开 —— 保证用户永远看得到界面，不会"打不开"。
static std::wstring ReadyPath() {
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    // 带 pid：多个实例不会互相覆盖，后端按 --watch 的 pid 找对应文件
    wchar_t name[64];
    swprintf(name, 64, L"\\OpenHack\\native_%lu.ready", GetCurrentProcessId());
    return std::wstring(appdata) + name;
}

static void WriteNativeReady() {
    FILE* f = _wfopen(ReadyPath().c_str(), L"wb");
    if (f) {
        fwprintf(f, L"%lu", GetCurrentProcessId());
        fclose(f);
    }
    Log(L"wv: page READY");
}

// 找浏览器目录：必须是**含 msedgewebview2.exe 的具体版本子目录**，
// 直接传 "...\Application" 会报 ERROR_FILE_NOT_FOUND(0x80070002)。
// 优先完整版 Edge —— 本机默认用的 WebView2 Runtime(137) 会让渲染进程崩溃，
// 而 Edge 自带的运行时版本更新(138)，实测能正常渲染。
static std::wstring FindBrowserFolder() {
    wchar_t buf[MAX_PATH]{};
    const wchar_t* vars[] = { L"ProgramFiles(x86)", L"ProgramFiles" };
    const wchar_t* products[] = { L"\\Microsoft\\Edge\\Application",
                                  L"\\Microsoft\\EdgeWebView\\Application" };
    for (const wchar_t* var : vars) {
        buf[0] = 0;
        if (!GetEnvironmentVariableW(var, buf, MAX_PATH)) continue;
        for (const wchar_t* rel : products) {
            std::wstring app = std::wstring(buf) + rel;
            std::wstring best;
            WIN32_FIND_DATAW fd{};
            HANDLE h = FindFirstFileW((app + L"\\*").c_str(), &fd);
            if (h == INVALID_HANDLE_VALUE) continue;
            do {
                if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
                std::wstring name = fd.cFileName;
                if (name.empty() || name[0] < L'0' || name[0] > L'9') continue;
                if (name > best) best = name;      // 取版本号最大的
            } while (FindNextFileW(h, &fd));
            FindClose(h);
            if (best.empty()) continue;
            std::wstring dir = app + L"\\" + best;
            if (GetFileAttributesW((dir + L"\\msedgewebview2.exe").c_str())
                != INVALID_FILE_ATTRIBUTES) {
                return dir;
            }
        }
    }
    return L"";
}

static std::wstring GetExeDir() {
    wchar_t buf[MAX_PATH]{};
    GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring p(buf);
    auto pos = p.find_last_of(L"\\/");
    return pos == std::wstring::npos ? L"." : p.substr(0, pos);
}

// ============================================================ 单文件自解压
// 后端(OpenHackCore.exe, 约 17MB)和 WebView2Loader.dll 以 footer 的形式
// 追加在本 exe 文件末尾，运行时按尾巴里的长度信息把它们解到
// %LOCALAPPDATA%\OpenHack\bin。这样对外只交付一个 OpenHack.exe。
//
// footer 布局（从文件末尾往前 32 字节）：
//   [coreLen:8][dllLen:8][buildId:8]["OHPK0001":8]
// 数据区在这 32 字节之前：先是 core，再是 dll。
//
// buildId 就是后端文件的 mtime，用来判断 bin 里那份是否已经过期。
// 只比文件大小是不够的 —— 两次构建可能刚好一样大，结果新版本永远装不进去。

static std::wstring PayloadDir() {
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    std::wstring base = std::wstring(appdata) + L"\\OpenHack";
    CreateDirectoryW(base.c_str(), nullptr);
    std::wstring bin = base + L"\\bin";
    CreateDirectoryW(bin.c_str(), nullptr);
    return bin;
}

static bool WriteSliceFromSelf(HANDLE self, unsigned long long offset,
                               unsigned long long len, const std::wstring& dst) {
    // 这里刻意不再用"文件大小一致就跳过"：
    // 两次构建的 exe 很可能大小完全相同，一旦跳过，bin 里就永远留着旧后端，
    // 用户明明换了新 exe 却还在跑旧逻辑（排查起来极其费劲）。
    // 是否跳过由调用方按 buildId 判断。
    HANDLE out = CreateFileW(dst.c_str(), GENERIC_WRITE, 0, nullptr,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (out == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER pos{};
    pos.QuadPart = (LONGLONG)offset;
    SetFilePointerEx(self, pos, nullptr, FILE_BEGIN);
    const DWORD BUF = 1 << 20;
    std::vector<char> buf(BUF);
    unsigned long long left = len;
    bool ok = true;
    while (left > 0) {
        DWORD want = (DWORD)(left < BUF ? left : BUF);
        DWORD got = 0;
        if (!ReadFile(self, buf.data(), want, &got, nullptr) || got == 0) { ok = false; break; }
        DWORD wr = 0;
        if (!WriteFile(out, buf.data(), got, &wr, nullptr) || wr != got) { ok = false; break; }
        left -= got;
    }
    CloseHandle(out);
    return ok;
}

// 启动前把占着 bin\OpenHackCore.exe 的旧后端清掉。
// 不清的话 CreateFileW 写不进去 → 新版本装不上 → 用户换了新 exe 却还在跑旧逻辑。
static void KillStaleCore() {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof(pe);
    const DWORD selfPid = GetCurrentProcessId();
    int killed = 0;
    if (Process32FirstW(snap, &pe)) {
        do {
            if (pe.th32ProcessID == selfPid) continue;
            if (_wcsicmp(pe.szExeFile, kCoreExe) != 0) continue;
            HANDLE hp = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE,
                                    pe.th32ProcessID);
            if (!hp) continue;
            TerminateProcess(hp, 0);
            WaitForSingleObject(hp, 3000);
            CloseHandle(hp);
            ++killed;
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    if (killed) {
        wchar_t b[80];
        swprintf(b, 80, L"payload: 已终止 %d 个残留后端", killed);
        Log(b);
        Sleep(400);
    }
}

static std::wstring BuildIdFile() {
    return PayloadDir() + L"\\.buildid";
}

static unsigned long long ReadStoredBuildId() {
    FILE* fp = _wfopen(BuildIdFile().c_str(), L"rb");
    if (!fp) return 0;
    char buf[64]{};
    size_t n = fread(buf, 1, sizeof(buf) - 1, fp);
    fclose(fp);
    if (!n) return 0;
    return _strtoui64(buf, nullptr, 10);
}

static void WriteStoredBuildId(unsigned long long id) {
    FILE* fp = _wfopen(BuildIdFile().c_str(), L"wb");
    if (!fp) return;
    fprintf(fp, "%llu", id);
    fclose(fp);
}

static bool ExtractPayload() {
    wchar_t self[MAX_PATH]{};
    GetModuleFileNameW(nullptr, self, MAX_PATH);
    HANDLE f = CreateFileW(self, GENERIC_READ, FILE_SHARE_READ, nullptr,
                          OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER sz{};
    GetFileSizeEx(f, &sz);
    const int FOOT = 32;
    bool ok = false;

    if (sz.QuadPart > FOOT) {
        LARGE_INTEGER pos{};
        pos.QuadPart = sz.QuadPart - FOOT;
        SetFilePointerEx(f, pos, nullptr, FILE_BEGIN);
        unsigned long long coreLen = 0, dllLen = 0, buildId = 0;
        char magic[8]{};
        DWORD got = 0;
        ReadFile(f, &coreLen, 8, &got, nullptr);
        ReadFile(f, &dllLen, 8, &got, nullptr);
        ReadFile(f, &buildId, 8, &got, nullptr);
        ReadFile(f, magic, 8, &got, nullptr);

        if (memcmp(magic, "OHPK0001", 8) != 0 || coreLen == 0) {
            Log(L"payload: 自身没有内嵌后端（分体模式）");
            CloseHandle(f);
            return false;
        }

        std::wstring dir = PayloadDir();
        std::wstring corePath = dir + L"\\OpenHackCore.exe";
        bool sameVersion = (ReadStoredBuildId() == buildId)
                        && PathFileExistsW(corePath.c_str());
        if (sameVersion) {
            Log(L"payload: 版本一致，跳过解压");
            CloseHandle(f);
            return true;
        }

        // 要更新：先把占着文件的旧后端清掉，否则写不进去
        KillStaleCore();

        unsigned long long start = (unsigned long long)sz.QuadPart
                                 - FOOT - coreLen - dllLen;
        ok = WriteSliceFromSelf(f, start, coreLen, corePath);
        if (ok && dllLen > 0) {
            WriteSliceFromSelf(f, start + coreLen, dllLen,
                               dir + L"\\WebView2Loader.dll");
        }
        if (ok) {
            WriteStoredBuildId(buildId);
            wchar_t b[96];
            swprintf(b, 96, L"payload: 已更新自带后端 (build %llu)", buildId);
            Log(b);
        } else {
            Log(L"payload: 解压失败 —— 文件被杀软或旧进程占用");
        }
    }
    CloseHandle(f);
    return ok;
}

static void EnableDpiAwareness() {
    // Per-Monitor V2：不然高分屏上字是糊的。
    // 注意参数是"指针大小"的句柄而不是 int —— 声明成 int 会让高 32 位是垃圾值，
    // 传进去之后行为未定义（曾经的崩溃点之一）。
    auto pSetCtx = reinterpret_cast<BOOL(WINAPI*)(LONG_PTR)>(
        GetProcAddress(GetModuleHandleW(L"user32.dll"),
                       "SetProcessDpiAwarenessContext"));
    if (pSetCtx) {
        pSetCtx((LONG_PTR)-4);   // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        Log(L"dpi: per-monitor v2");
    }
}

// ------------------------------------------------- 毛玻璃（本项目核心）

static void ApplyGlass(HWND hwnd) {
    // 1) Win11 官方 backdrop，效果最正
    auto pDwm = reinterpret_cast<FnDwmSetWindowAttribute>(
        GetProcAddress(LoadLibraryW(L"dwmapi.dll"), "DwmSetWindowAttribute"));
    if (pDwm) {
        DWORD v = kDwmsbtTransient;
        if (SUCCEEDED(pDwm(hwnd, kDwmwaSystemBackdrop, &v, sizeof(v)))) {
            Log(L"glass: win11 backdrop (acrylic)");
            return;
        }
    }

    // 2) Win10 的 accent —— ExplorerBlurMica 在 Win10 上用的就是这一条
    auto pSet = reinterpret_cast<FnSetWindowCompositionAttribute>(
        GetProcAddress(GetModuleHandleW(L"user32.dll"),
                       "SetWindowCompositionAttribute"));
    if (pSet) {
        // 0x66FFFFFF ≈ 40% 白：既透出桌面，又让浅色界面上的深色文字还读得清
        ACCENT_POLICY policy{ kAccentAcrylic, 2, 0x66FFFFFF, 0 };
        WINCOMPATTRDATA data{ kWcaAccentPolicy, &policy, sizeof(policy) };
        if (pSet(hwnd, &data)) {
            Log(L"glass: win10 accent acrylic");
            return;
        }
        ACCENT_POLICY p2{ kAccentBlurBehind, 2, 0x99000000, 0 };
        WINCOMPATTRDATA d2{ kWcaAccentPolicy, &p2, sizeof(p2) };
        if (pSet(hwnd, &d2)) {
            Log(L"glass: legacy blur");
            return;
        }
    }
    Log(L"glass: NO material applied");
}

// ============================================================ COM 回调外壳
// 不依赖 WRL/ATL（mingw 没有），手写 IUnknown 三个方法即可。

template <class Iface>
struct ComObj : public Iface {
    ULONG ref_ = 1;
    // Release() 里会 delete this，必须有虚析构，否则 clang 会警告
    virtual ~ComObj() = default;
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (IsEqualIID(riid, IID_IUnknown)) {
            *ppv = static_cast<IUnknown*>(static_cast<Iface*>(this));
        } else if (IsEqualIID(riid, ThisIID())) {
            *ppv = static_cast<Iface*>(this);
        } else {
            *ppv = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    virtual REFIID ThisIID() const = 0;
};

// ------------------------------------------------- 拖动窗口
// WebView2 没有 Electron 的 -webkit-app-region，所以由页面 JS 判断是否该拖，
// 这里负责真正发起"拖标题栏" —— 既保住拖拽，又不吞掉按钮的点击。

static void BeginCaptionDrag(HWND hwnd) {
    ReleaseCapture();
    SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0);
}

// ------------------------------------------------- 接收页面消息

struct WebMessageHandler : ComObj<ICoreWebView2WebMessageReceivedEventHandler> {
    HWND hwnd{};
    REFIID ThisIID() const override {
        return IID_ICoreWebView2WebMessageReceivedEventHandler;
    }
    STDMETHODIMP Invoke(ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs* args) override {
        LPWSTR msg = nullptr;
        if (SUCCEEDED(args->get_WebMessageAsJson(&msg)) && msg) {
            std::wstring s(msg);
            CoTaskMemFree(msg);
            if (s.find(L"__drag") != std::wstring::npos) BeginCaptionDrag(hwnd);
            else if (s.find(L"__ready") != std::wstring::npos) WriteNativeReady();
        }
        return S_OK;
    }
};

// ------------------------------------------------- Controller 创建完成

struct ControllerHandler
    : ComObj<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler> {
    HWND hwnd{};
    std::wstring url;
    REFIID ThisIID() const override {
        return IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler;
    }
    STDMETHODIMP Invoke(HRESULT res, ICoreWebView2Controller* controller) override;
};

STDMETHODIMP ControllerHandler::Invoke(HRESULT res,
                                       ICoreWebView2Controller* controller) {
    if (FAILED(res) || !controller) {
        { wchar_t b[64]; swprintf(b, 64, L"wv: controller FAILED hr=0x%08X", res); Log(b); }
        return S_OK;
    }
    Log(L"wv: controller created");
    g_controller = controller;
    controller->AddRef();

    ICoreWebView2* webview = nullptr;
    controller->get_CoreWebView2(&webview);
    if (!webview) { Log(L"wv: get_CoreWebView2 返回空"); return S_OK; }
    Log(L"wv: got webview");

    // 关键一步：把 WebView2 自己的默认底色设成全透明。
    // 否则它会整块刷白，把窗口背后的玻璃材质完全盖住。
    ICoreWebView2Controller2* ctrl2 = nullptr;
    if (SUCCEEDED(controller->QueryInterface(
            IID_ICoreWebView2Controller2, reinterpret_cast<void**>(&ctrl2))) && ctrl2) {
        COREWEBVIEW2_COLOR transparent{ 0, 0, 0, 0 };
        ctrl2->put_DefaultBackgroundColor(transparent);
        ctrl2->Release();
        Log(L"wv: background = transparent");
    } else {
        Log(L"wv: 无 Controller2，无法设透明背景");
    }

    RECT rc{};
    GetClientRect(hwnd, &rc);
    controller->put_Bounds(rc);
    Log(L"wv: bounds set");

    // --no-inject：跳过消息回调与脚本注入，用来二分"崩溃是不是我自己的 COM 回调引起的"
    bool noInject = wcsstr(GetCommandLineW(), L"--no-inject") != nullptr;
    if (noInject) {
        Log(L"wv: --no-inject（跳过消息回调与脚本注入）");
    } else {
        auto* mh = new WebMessageHandler();
        mh->hwnd = hwnd;
        EventRegistrationToken tok{};
        // 引用交给 SDK，注册期间由它持有，不要自己 Release
        webview->add_WebMessageReceived(mh, &tok);

        // 让页面进入"原生玻璃"模式：底色透明，由 CSS 的 .native-glass 接管
        webview->AddScriptToExecuteOnDocumentCreated(
            L"(function(){try{"
            L" if(!window.chrome||!window.chrome.webview)return;"
            L" document.documentElement.style.background='transparent';"
            L" var on=function(){document.body&&document.body.classList.add('native-glass');};"
            L" if(document.body)on();"
            L" document.addEventListener('DOMContentLoaded',on);"
            L" window.addEventListener('load',function(){"
            L"   try{window.chrome.webview.postMessage('__ready');}catch(e){}"
            L" });"
            L"}catch(e){}})();",
            nullptr);
    }

    // --nav-blank：只导航 about:blank，用来判断崩溃是"渲染进程起不来"
    // 还是"页面里的重合成效果（backdrop-filter）在无 GPU 环境下把渲染进程带崩"
    const wchar_t* target = url.c_str();
    if (wcsstr(GetCommandLineW(), L"--nav-blank") != nullptr) {
        target = L"about:blank";
        Log(L"wv: nav-blank 模式");
    }
    Log(L"wv: navigating");
    webview->Navigate(target);
    Log(L"wv: navigated");
    webview->Release();
    return S_OK;
}

// ------------------------------------------------- Environment 创建完成

struct EnvHandler
    : ComObj<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler> {
    HWND hwnd{};
    std::wstring url;
    REFIID ThisIID() const override {
        return IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler;
    }
    STDMETHODIMP Invoke(HRESULT res, ICoreWebView2Environment* env) override {
        if (FAILED(res) || !env) {
            { wchar_t b[64]; swprintf(b, 64, L"wv: env FAILED hr=0x%08X", res); Log(b); }
            // 起不来就干脆关掉窗口：后端在监视本进程，发现"进程没了且页面没 ready"
            // 会自动用系统浏览器把界面打开 —— 用户至少不会遇到"双击什么都没有"。
            Log(L"wv: 关闭窗口，交给后端回退");
            PostMessageW(hwnd, WM_CLOSE, 0, 0);
            return S_OK;
        }
        Log(L"wv: environment created");
        auto* h = new ControllerHandler();
        h->hwnd = hwnd;
        h->url = url;
        // 注意：这里不能自己 Release。
        // new 出来的这一份引用是交给 SDK 的 —— 异步完成后由 SDK 释放。
        // 以前在这后面又调了一次 Release，等于提前把对象销毁，
        // SDK 后面再来用就是访问已释放内存，直接 0xC0000005。
        env->CreateCoreWebView2Controller(hwnd, h);
        env->Release();
        return S_OK;
    }
};

// ============================================================ 初始化 WebView2

using FnCreateEnv = HRESULT(STDAPICALLTYPE*)(
    PCWSTR, PCWSTR, ICoreWebView2EnvironmentOptions*,
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*);

static bool InitWebView(HWND hwnd, const std::wstring& url) {
    // 单文件模式下 loader 在自解压目录，得用绝对路径加载
    HMODULE loader = nullptr;
    std::wstring dllInPayload = PayloadDir() + L"\\WebView2Loader.dll";
    if (PathFileExistsW(dllInPayload.c_str())) {
        loader = LoadLibraryW(dllInPayload.c_str());
    }
    if (!loader) loader = LoadLibraryW(L"WebView2Loader.dll");
    if (!loader) {
        MessageBoxW(hwnd,
            L"缺少 WebView2Loader.dll。\n请把它和本程序放在同一个文件夹里。",
            L"OpenHack", MB_ICONERROR);
        return false;
    }
    auto pCreate = reinterpret_cast<FnCreateEnv>(
        GetProcAddress(loader, "CreateCoreWebView2EnvironmentWithOptions"));
    if (!pCreate) {
        OutputDebugStringW(L"[wv] loader entry point missing\n");
        return false;
    }

    // 关掉 GPU 进程：在没有真实显卡/远程会话这类环境里，
    // Chromium 的 GPU 进程起不来会直接把宿主进程带崩（实测 0xC0000005）。
    ICoreWebView2EnvironmentOptions* opts = nullptr;
    auto pMakeOpts = reinterpret_cast<HRESULT(STDAPICALLTYPE*)(ICoreWebView2EnvironmentOptions**)>(
        GetProcAddress(loader, "CreateCoreWebView2EnvironmentOptions"));
    if (pMakeOpts && SUCCEEDED(pMakeOpts(&opts)) && opts) {
        // --no-sandbox 很关键：Chromium 的沙箱在受限/被安全软件管着的机器上会直接把
        // 浏览器进程带崩（表现就是"双击后一闪就没了"）。
        opts->put_AdditionalBrowserArguments(
            L"--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage");
        Log(L"wv: args = --no-sandbox --disable-gpu ...");
    }

    // 用户数据目录独立，避免和别的 WebView2 实例抢同一份 profile
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    std::wstring udf = std::wstring(appdata) + L"\\OpenHack\\wv2";

    auto* h = new EnvHandler();
    h->hwnd = hwnd;
    h->url = url;
    // 同上：这一份引用归 SDK，异步完成后由它释放
    // 优先用完整版 Edge 当浏览器：本机 WebView2 Runtime 的渲染进程起不来，
    // 换 Edge 就能正常渲染（SDK 支持通过 browserExecutableFolder 指定）。
    // 注意：这里刻意传 nullptr（用系统默认的 WebView2 Runtime）。
    // 试过指到已安装的 Edge Stable(138) 目录，WebView2 明确不支持
    // （返回 ERROR_NOT_SUPPORTED / 0x80070032），官方只允许固定版本运行时。
    // 万一这个 Runtime 起不来、进程崩了，由后端兜底回退到系统浏览器。
    std::wstring browser = FindBrowserFolder();
    Log(browser.empty() ? L"wv: browser = 系统默认 WebView2 Runtime"
                        : L"wv: browser = 系统默认（已检测到 Edge，但 Stable 不可用作运行时）");
    HRESULT hr = pCreate(nullptr, udf.c_str(), opts, h);
    { wchar_t b[64]; swprintf(b, 64, L"wv: pCreate hr=0x%08X", hr); Log(b); }
    if (opts) opts->Release();
    return SUCCEEDED(hr);
}

// ============================================================ 拉起后端 / 取端口

// 当前装着的 WebView2 Runtime 版本（EdgeWebView\Application 下最大的版本目录）
static std::wstring Wv2RuntimeVersion() {
    wchar_t buf[MAX_PATH]{};
    for (const wchar_t* var : { L"ProgramFiles(x86)", L"ProgramFiles" }) {
        buf[0] = 0;
        if (!GetEnvironmentVariableW(var, buf, MAX_PATH)) continue;
        std::wstring app = std::wstring(buf) + L"\\Microsoft\\EdgeWebView\\Application";
        std::wstring best;
        WIN32_FIND_DATAW fd{};
        HANDLE h = FindFirstFileW((app + L"\\*").c_str(), &fd);
        if (h == INVALID_HANDLE_VALUE) continue;
        do {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            std::wstring n = fd.cFileName;
            if (n.empty() || n[0] < L'0' || n[0] > L'9') continue;
            if (n > best) best = n;
        } while (FindNextFileW(h, &fd));
        FindClose(h);
        if (!best.empty()) return best;
    }
    return L"";
}

static std::wstring BrokenMarkPath() {
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    std::wstring d = std::wstring(appdata) + L"\\OpenHack";
    CreateDirectoryW(d.c_str(), nullptr);
    return d + L"\\wv2_unsupported.txt";
}

// 上次在这台机器上渲染时 WebView2 崩过、且 Runtime 版本没变 → 不再白试一遍。
// 这样用户不会每次双击都先闪一个窗口再跳浏览器；等 WebView2 升级了版本号一变，
// 就自动重新尝试原生渲染。
static bool Wv2KnownBroken() {
    std::wstring cur = Wv2RuntimeVersion();
    if (cur.empty()) return false;
    FILE* f = _wfopen(BrokenMarkPath().c_str(), L"rb");
    if (!f) return false;
    char buf[128]{};
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    std::string mark(buf, n);
    while (!mark.empty() && (mark.back() == '\r' || mark.back() == '\n' ||
                             mark.back() == ' ' || mark.back() == '\t')) {
        mark.pop_back();
    }
    std::string curA(cur.begin(), cur.end());
    return mark == curA;
}

static bool StartCore(bool headless = true, bool watch = true) {
    // 单文件模式：后端在自解压目录；分体/开发模式：和 exe 放同一目录
    std::wstring core = GetExeDir() + L"\\" + kCoreExe;
    if (!PathFileExistsW(core.c_str())) {
        std::wstring inPayload = PayloadDir() + L"\\" + kCoreExe;
        if (PathFileExistsW(inPayload.c_str())) {
            core = inPayload;
        } else {
            std::wstring sub = GetExeDir() + L"\\core\\" + kCoreExe;
            if (PathFileExistsW(sub.c_str())) core = sub;
            else {
                MessageBoxW(nullptr, (L"找不到后端程序：\n" + core).c_str(),
                            L"OpenHack", MB_ICONERROR);
                return false;
            }
        }
    }

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;

    // 把自己的 pid 告诉后端，让它盯着：一旦本进程在页面 ready 前就死了，
    // 后端会自己用浏览器把界面打开，不至于让用户"双击后什么都没有"。
    std::wstring cmd = L"\"" + core + L"\"";
    if (headless) cmd += L" --no-browser";
    if (watch) {
        wchar_t pidbuf[48];
        swprintf(pidbuf, 48, L" --watch %lu", GetCurrentProcessId());
        cmd += pidbuf;
    }
    std::vector<wchar_t> buf(cmd.begin(), cmd.end());
    buf.push_back(L'\0');

    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(nullptr, buf.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        MessageBoxW(nullptr, L"后端启动失败。", L"OpenHack", MB_ICONERROR);
        return false;
    }
    g_child = pi;
    g_hasChild = true;
    return true;
}

static bool ReadPort(int& port) {
    wchar_t appdata[MAX_PATH]{};
    GetEnvironmentVariableW(L"LOCALAPPDATA", appdata, MAX_PATH);
    std::wstring path = std::wstring(appdata) + L"\\OpenHack\\data\\server.ready";
    for (int i = 0; i < 80; ++i) {
        HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                               nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (f != INVALID_HANDLE_VALUE) {
            char buf[32]{};
            DWORD got = 0;
            if (ReadFile(f, buf, sizeof(buf) - 1, &got, nullptr) && got > 0) {
                CloseHandle(f);
                int p = atoi(buf);
                if (p > 0) { port = p; return true; }
            }
            CloseHandle(f);
        }
        Sleep(400);
    }
    return false;
}

// ============================================================ 窗口过程

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_NCCALCSIZE:
        // 吃掉非客户区：无边框，但仍保留 WS_THICKFRAME 带来的缩放能力
        return 0;

    case WM_NCHITTEST: {
        // 只把四周 kBorderHit 像素认作缩放边框，其余交给 WebView2（HTCLIENT）。
        // 顶栏拖拽不在这里处理 —— 由页面 JS 发消息触发，否则会吞掉按钮点击。
        POINT pt{ GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
        RECT rc{};
        GetWindowRect(hwnd, &rc);
        bool l = pt.x < rc.left + kBorderHit;
        bool r = pt.x >= rc.right - kBorderHit;
        bool t = pt.y < rc.top + kBorderHit;
        bool b = pt.y >= rc.bottom - kBorderHit;
        if (l && t) return HTTOPLEFT;
        if (r && t) return HTTOPRIGHT;
        if (l && b) return HTBOTTOMLEFT;
        if (r && b) return HTBOTTOMRIGHT;
        if (l) return HTLEFT;
        if (r) return HTRIGHT;
        if (t) return HTTOP;
        if (b) return HTBOTTOM;
        return HTCLIENT;
    }

    case WM_ERASEBKGND:
        return 1;   // 不自己刷底色，让 DWM 的材质透出来

    case WM_SIZE:
        if (g_controller) {
            RECT rc{};
            GetClientRect(hwnd, &rc);
            g_controller->put_Bounds(rc);
        }
        return 0;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;

    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

// ============================================================ WinMain

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE, PWSTR, int) {
    Log(L"main: start");
    EnableDpiAwareness();
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // 本机已确认 WebView2 渲染会崩（且 Runtime 版本没变）：直接让后端自己开浏览器。
    // 少一次"先闪个窗口再跳走"的糟糕体验；等 WebView2 升级后会自动重新尝试原生。
    // 单文件模式：先把随包的后端解出来（版本一致会自动跳过）
    bool payloadOk = ExtractPayload();
    bool sideBySide = PathFileExistsW((GetExeDir() + L"\\" + kCoreExe).c_str());
    if (!payloadOk && !sideBySide) {
        // 绝不能静默继续：那样会悄悄跑起 bin 里的旧后端，
        // 表现就是"明明换了新 exe，毛病却一点没变"，极难排查。
        MessageBoxW(nullptr,
            L"内置后端解压失败。\n\n"
            L"通常是杀毒软件（360 / Windows Defender）正在占用文件，"
            L"或上一次的后端进程还没退出。\n\n"
            L"请等几秒后重试；若反复出现，把本程序加入杀毒软件白名单。",
            L"OpenHack", MB_ICONERROR);
        CoUninitialize();
        return 1;
    }

    // --force-webview：无视"已知不可用"的标记，强行再试一次原生渲染（排查用）
    bool forceWv = wcsstr(GetCommandLineW(), L"--force-webview") != nullptr;
    if (!forceWv && Wv2KnownBroken()) {
        Log(L"wv2: 已知本机 WebView2 不可用且版本未变 → 直接走浏览器");
        if (!StartCore(false, false)) { CoUninitialize(); return 1; }
        CoUninitialize();
        return 0;   // 后端是独立进程，会继续跑并把界面打开
    }

    if (!StartCore()) { Log(L"core: FAILED to start"); CoUninitialize(); return 1; }
    Log(L"core: started");

    if (!ReadPort(g_port)) {
        Log(L"core: FAILED to read port");
        MessageBoxW(nullptr, L"后端服务没有在预期时间内就绪。",
                    L"OpenHack", MB_ICONERROR);
        if (g_hasChild) TerminateProcess(g_child.hProcess, 1);
        CoUninitialize();
        return 1;
    }

    { wchar_t b[64]; swprintf(b, 64, L"core: port %d", g_port); Log(b); }

    const wchar_t* cls = L"OpenHackNativeWindow";
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = WndProc;
    wc.hInstance = hInst;
    wc.lpszClassName = cls;
    wc.hbrBackground = nullptr;              // 不带背景画刷
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    RegisterClassExW(&wc);

    g_hwnd = CreateWindowExW(
        0, cls, L"OpenHack · 黑苹果一键装机",
        WS_POPUP | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 1320, 900,
        nullptr, nullptr, hInst, nullptr);
    if (!g_hwnd) {
        MessageBoxW(nullptr, L"窗口创建失败。", L"OpenHack", MB_ICONERROR);
        if (g_hasChild) TerminateProcess(g_child.hProcess, 1);
        CoUninitialize();
        return 1;
    }

    Log(L"window: created");
    ApplyGlass(g_hwnd);

    ShowWindow(g_hwnd, SW_SHOW);
    UpdateWindow(g_hwnd);
    Log(L"window: shown");

    wchar_t urlBuf[128];
    swprintf(urlBuf, 128, L"http://127.0.0.1:%d/", g_port);
    // --no-webview：只开窗口、不上 WebView2。用来隔离"窗口/毛玻璃"与"WebView2"两部分，
    // 排查崩溃到底是哪一个引起的。
    bool noWv = wcsstr(GetCommandLineW(), L"--no-webview") != nullptr;
    if (noWv) {
        Log(L"webview: SKIPPED (--no-webview)");
    } else {
        Log(L"webview: init begin");
        InitWebView(g_hwnd, urlBuf);
        Log(L"webview: init returned");
    }

    // 后端进程一退出就关窗口（页面里的"退出"按钮会让它自己退）
    std::thread([] {
        if (g_hasChild) {
            WaitForSingleObject(g_child.hProcess, INFINITE);
            PostMessageW(g_hwnd, WM_CLOSE, 0, 0);
        }
    }).detach();

    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (g_hasChild) {
        TerminateProcess(g_child.hProcess, 0);
        CloseHandle(g_child.hThread);
        CloseHandle(g_child.hProcess);
    }
    if (g_controller) g_controller->Release();
    CoUninitialize();
    return (int)msg.wParam;
}
