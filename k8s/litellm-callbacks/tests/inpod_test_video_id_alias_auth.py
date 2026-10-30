"""在真 litellm pod 里跑：用真的 auth 函数 + 假 token，测 video_id_alias_auth 补丁。

不改任何配置；不发外部请求。只调用 litellm 自己的鉴权函数，判据 = 抛不抛 key_model_access_denied。
用法：python3 test_video_alias_auth.py <补丁文件路径> [--no-patch]
"""
import asyncio, base64, importlib.util, sys

PATCH = sys.argv[1]
NO_PATCH = "--no-patch" in sys.argv

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth import auth_utils
from litellm.types.videos.utils import encode_video_id_with_provider

if not NO_PATCH:
    spec = importlib.util.spec_from_file_location("video_id_alias_auth", PATCH)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m._INSTALLED, "patch did not install"

from litellm.proxy.auth import user_api_key_auth as uak


import litellm
# 真 Router，两个视频部署，id 照抄生产；不发任何请求，只用它做 id->组名反解和鉴权里的查询
router = litellm.Router(model_list=[
    {"model_name": "web-gemini-video", "litellm_params": {"model": "openai/gemini-advanced", "api_base": "http://127.0.0.1:9/v1", "api_key": "x"},
     "model_info": {"id": "gemini-media/video"}},
    {"model_name": "muse-video", "litellm_params": {"model": "openai/muse-video", "api_base": "http://127.0.0.1:9/v1", "api_key": "x"},
     "model_info": {"id": "muse2api-muse-video"}},
])
assert router.resolve_model_name_from_model_id("gemini-media/video") == "web-gemini-video"
vid_web = encode_video_id_with_provider("video_x1", "openai", "gemini-media/video")
vid_muse = encode_video_id_with_provider("video_x2", "openai", "muse2api-muse-video")


def tok(models, aliases):
    return UserAPIKeyAuth(api_key="sk-test", token="t" * 64, models=models, aliases=aliases)


async def check(token, video_id, route):
    data = {"video_id": video_id}
    model = auth_utils.get_model_from_request(request_data=data, route=route, llm_router=router)
    try:
        await uak.can_key_call_model(model=model, llm_model_list=None, valid_token=token, llm_router=router)
        return "ALLOW", model
    except Exception as e:
        kind = "DENY" if ("key_model_access_denied" in repr(getattr(e, "type", "")) + str(e) or type(e).__name__ == "ProxyException") else "ERROR"
        return "%s(%s: %s)" % (kind, type(e).__name__, str(e)[:70]), model


CASES = [
    # (说明, token, video_id, 期望)
    ("std 形状：允许列表只有对外名 gemini-video + 别名 → 查进度",
     tok(["gemini-video"], {"gemini-video": "web-gemini-video"}), vid_web, "ALLOW"),
    ("同上 → 下载 content 路由",
     tok(["gemini-video"], {"gemini-video": "web-gemini-video"}), vid_web, "ALLOW"),
    ("🔒 没有任何视频名的 key，不许被放行",
     tok(["gpt-6-astra"], {}), vid_web, "DENY"),
    ("🔒 有别名但对外名不在允许列表（别名 ≠ 授权）",
     tok(["gpt-6-astra"], {"gemini-video": "web-gemini-video"}), vid_web, "DENY"),
    ("老形状：对外名 + 内部名都在允许列表（不能被改坏）",
     tok(["gemini-video", "web-gemini-video"], {"gemini-video": "web-gemini-video"}), vid_web, "ALLOW"),
    ("同名组 muse-video 直接在允许列表（无别名）",
     tok(["muse-video"], {}), vid_muse, "ALLOW"),
    ("🔒 有 gemini-video 的 key 去查 muse 的视频（不该借道放行）",
     tok(["gemini-video"], {"gemini-video": "web-gemini-video"}), vid_muse, "DENY"),
]


async def main():
    routes = ["/v1/videos/" + vid_web, "/v1/videos/" + vid_web + "/content"]
    fails = 0
    for i, (desc, t, vid, want) in enumerate(CASES):
        route = routes[1] if "content" in desc else "/v1/videos/" + vid
        got, model = await check(t, vid, route)
        ok = got.startswith(want)
        fails += 0 if ok else 1
        print("%s  %-46s 期望=%-5s 实际=%-30s 候选=%r" % ("PASS" if ok else "FAIL", desc[:46], want, got, model))
    # 普通请求体里的 model 不能被替换（标记只来自资源 id）
    t = tok(["gemini-video"], {"gemini-video": "web-gemini-video"})
    try:
        mm = auth_utils.get_model_from_request(request_data={"model": "web-gemini-video"}, route="/v1/chat/completions", llm_router=router)
        await uak.can_key_call_model(model=mm, llm_model_list=None, valid_token=t, llm_router=router)
        got = "ALLOW"
    except Exception as e:
        got = "DENY"
    ok = got == "DENY"; fails += 0 if ok else 1
    print("%s  %-46s 期望=DENY  实际=%s" % ("PASS" if ok else "FAIL", "🔒 请求体直接写内部名 web-gemini-video 不被替换", got))
    print("\n%s：%d FAIL" % ("未打补丁(阴性对照)" if NO_PATCH else "打了补丁", fails))


asyncio.run(main())
