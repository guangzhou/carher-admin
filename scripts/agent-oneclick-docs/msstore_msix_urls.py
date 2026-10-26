#!/usr/bin/env python3
"""微软商店应用 → 官方 MSIX 直链(只读,不下载)。给 app-mirror-sync.sh 用。

为什么要有它(2026-10-26):Codex 的 Windows 版只在微软商店上(产品 9PLM9XGG6VKS),
官网给的是一个 800KB 的「ChatGPT Installer.exe」在线安装器,它运行时再去微软 CDN 拉 1GB 的 msix ——
内网/国内同事经常卡在这一步,也没法放到我们自己的服务器上。
第三方解析站(store.rg-adguard.net)挂了 Cloudflare 人机验证,脚本过不去 ⇒ 直接走微软自己那套接口:

  1. displaycatalog(公开 JSON):产品 → WuCategoryId + 每个 MSIX 的 PackageFullName / 大小
  2. FE3 Windows Update SOAP(fe3cr.delivery.mp.microsoft.com,匿名):
       GetCookie → SyncUpdates(WuCategoryId) 拿到每个包的 UpdateID/RevisionNumber
       → GetExtendedUpdateInfo2(secured)拿到 tlu.dl.delivery.mp.microsoft.com 的直链
  这就是 winget / rg-adguard 背后用的同一套,链接本身是微软 CDN、带签名的官方原包。

用法:
  python3 msstore_msix_urls.py 9PLM9XGG6VKS            # 打印 JSON:[{name,arch,version,size,url}]
  python3 msstore_msix_urls.py 9PLM9XGG6VKS --arch x64 # 只要 x64

只用标准库;直链有时效(几小时),拿到就下,别存起来当永久链接。
"""
import json
import re
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

FE3 = "https://fe3cr.delivery.mp.microsoft.com/ClientWebService/client.asmx"
UA = "Windows-Update-Agent/10.0.10011.16384 Client-Protocol/2.50"
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "w": "http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService",
}


# FE3 的证书由「Microsoft ECC Update Secure Server CA 2.1」签发 —— 这是 Windows 内置、公共 CA 包里没有的
# 私有根(Linux/mac 的 curl/python 都验不过,Windows Update 客户端自带信任)。⇒ **只对 FE3 这一个主机**
# 跳过链校验;它返回的只是下载地址,文件本身来自 tlu.dl.delivery.mp.microsoft.com,下载后再按 MSIX
# 自带的 Authenticode 签名 + 目录里的大小核对,不信任这一跳的内容。其余主机照常完整校验。
import ssl
_FE3_CTX = ssl.create_default_context()
_FE3_CTX.check_hostname = False
_FE3_CTX.verify_mode = ssl.CERT_NONE


def http(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    ctx = _FE3_CTX if url.startswith(FE3) else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def catalog(product_id):
    url = (f"https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds={product_id}"
           "&market=US&languages=en-US")
    p = json.loads(http(url))["Products"][0]
    sku = p["DisplaySkuAvailabilities"][0]["Sku"]["Properties"]
    cat = json.loads(sku["FulfillmentData"]) if isinstance(sku["FulfillmentData"], str) else sku["FulfillmentData"]
    pkgs = {k["PackageFullName"]: k for k in (sku.get("Packages") or [])}
    return cat["WuCategoryId"], pkgs


def soap(action, body, secured=False):
    now = datetime.now(timezone.utc)
    ts = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = FE3 + ("/secured" if secured else "")
    env = f"""<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
<s:Header>
<a:Action s:mustUnderstand="1">http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/{action}</a:Action>
<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
<a:To s:mustUnderstand="1">{url}</a:To>
<o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<Timestamp xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
<Created>{ts(now)}</Created><Expires>{ts(now + timedelta(minutes=5))}</Expires></Timestamp>
<wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:wuws="http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization">
<TicketType Name="AAD" Version="1.0" Policy="MBI_SSL"></TicketType></wuws:WindowsUpdateTicketsToken>
</o:Security></s:Header>
<s:Body>{body}</s:Body></s:Envelope>"""
    return ET.fromstring(http(url, env.encode(), {"Content-Type": "application/soap+xml; charset=utf-8"}))


def get_cookie():
    r = soap("GetCookie", '<GetCookie xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">'
                          '<oldCookie></oldCookie><lastChange>2015-10-21T17:01:07.1472913Z</lastChange>'
                          '<currentTime>2015-10-21T17:01:07.1472913Z</currentTime><protocolVersion>1.40</protocolVersion>'
                          '</GetCookie>')
    return r.find(".//w:EncryptedData", NS).text


def sync_updates(cookie, category_id):
    # 已安装列表给一组常见的 Windows 基线 id,FE3 才会把商店应用的更新吐出来(winget/rg-adguard 同款写法)
    installed = "".join(f"<int>{i}</int>" for i in (
        1, 2, 3, 11, 19, 544, 549, 2359974, 2359977, 5169044, 8788830, 23110993, 23110994, 54341900, 54343656,
        59830006, 59830007, 59830008, 60484010, 62450018, 62450019, 62450020, 66027979, 66053150, 97657898,
        98822896, 98959022, 98959023, 98959024, 98959025, 98959026, 104433538, 104900364, 105489019, 117765322,
        129905029, 130040031, 132387090, 132393049, 133399034, 138537048, 140377312, 143747671, 158941041,
        158941042, 158941043, 158941044, 159123858, 159130928, 164836897, 164847386, 164848327, 164852241,
        164852246, 164852252, 164852253))
    body = f"""<SyncUpdates xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
<cookie><Expiration>2045-03-11T02:02:48Z</Expiration><EncryptedData>{cookie}</EncryptedData></cookie>
<parameters><ExpressQuery>false</ExpressQuery>
<InstalledNonLeafUpdateIDs>{installed}</InstalledNonLeafUpdateIDs>
<OtherCachedUpdateIDs></OtherCachedUpdateIDs><SkipSoftwareSync>false</SkipSoftwareSync><NeedTwoGroupOutOfScopeUpdates>true</NeedTwoGroupOutOfScopeUpdates>
<FilterAppCategoryIds><CategoryIdentifier><Id>{category_id}</Id></CategoryIdentifier></FilterAppCategoryIds>
<TreatAppCategoryIdsAsInstalled>true</TreatAppCategoryIdsAsInstalled><AlsoPerformRegularSync>false</AlsoPerformRegularSync>
<ComputerSpec/><ExtendedUpdateInfoParameters><XmlUpdateFragmentTypes><XmlUpdateFragmentType>Extended</XmlUpdateFragmentType></XmlUpdateFragmentTypes>
<Locales><string>en-US</string><string>en</string></Locales></ExtendedUpdateInfoParameters>
<ClientPreferredLanguages><string>en-US</string></ClientPreferredLanguages>
<ProductsParameters><SyncCurrentVersionOnly>false</SyncCurrentVersionOnly><DeviceAttributes>BranchReadinessLevel=CB;CurrentBranch=rs_prerelease;OEMModel=Virtual Machine;FlightRing=Retail;AttrDataVer=21;SystemManufacturer=Microsoft Corporation;InstallLanguage=en-US;OSUILocale=en-US;InstallationType=Client;FlightingBranchName=external;FirmwareVersion=Hyper-V UEFI Release v2.5;SystemProductName=Virtual Machine;OSSkuId=48;FlightContent=Branch;App=WU;OEMName_Uncleaned=Microsoft Corporation;AppVer=10.0.22621.900;OSArchitecture=AMD64;SystemSKU=None;UpdateManagementGroup=2;IsFlightingEnabled=1;IsDeviceRetailDemo=0;TelemetryLevel=3;OSVersion=10.0.22621.900;DeviceFamily=Windows.Desktop;</DeviceAttributes>
<CallerAttributes>Interactive=1;IsSeeker=0;</CallerAttributes><Products/></ProductsParameters></parameters></SyncUpdates>"""
    r = soap("SyncUpdates", body)
    # NewUpdates/UpdateInfo 给 ID→(UpdateID,Revision);ExtendedUpdateInfo/Updates 给 ID→文件名(PackageFullName 在 Xml 里)
    ids = {}
    for u in r.iterfind(".//w:NewUpdates/w:UpdateInfo", NS):
        x = ET.fromstring("<r>" + u.find("w:Xml", NS).text + "</r>")
        ident = x.find(".//UpdateIdentity")
        if ident is not None and x.find(".//SecuredFragment") is not None:
            ids[u.find("w:ID", NS).text] = (ident.get("UpdateID"), ident.get("RevisionNumber"))
    # 文件名是随机 GUID(`ca58e8d2-….msix`),认不出是哪个包 ⇒ 取 <File> 的字节数和 SHA1 Digest,
    #   字节数与 displaycatalog 的 MaxDownloadSizeInBytes 逐字节对上的才是那个架构的主包(10-26 实测 x64/arm64 各一)
    files = {}
    for u in r.iterfind(".//w:ExtendedUpdateInfo/w:Updates/w:Update", NS):
        x = u.find("w:Xml", NS).text or ""
        for f in re.finditer(r'<File FileName="([^"]+\.(?:msix|appx|msixbundle|appxbundle))" Digest="([^"]+)"[^>]*?Size="(\d+)"', x):
            files[u.find("w:ID", NS).text] = (f.group(1), f.group(2), int(f.group(3)))
    return [(files[k], uid, rev) for k, (uid, rev) in ids.items() if k in files]


def file_url(update_id, revision, digest=None):
    body = f"""<GetExtendedUpdateInfo2 xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
<updateIDs><UpdateIdentity><UpdateID>{update_id}</UpdateID><RevisionNumber>{revision}</RevisionNumber></UpdateIdentity></updateIDs>
<infoTypes><XmlUpdateFragmentType>FileUrl</XmlUpdateFragmentType><XmlUpdateFragmentType>FileDecryption</XmlUpdateFragmentType></infoTypes>
<deviceAttributes>BranchReadinessLevel=CB;CurrentBranch=rs_prerelease;OEMModel=Virtual Machine;FlightRing=Retail;AttrDataVer=21;SystemManufacturer=Microsoft Corporation;InstallLanguage=en-US;OSUILocale=en-US;InstallationType=Client;FlightingBranchName=external;FirmwareVersion=Hyper-V UEFI Release v2.5;SystemProductName=Virtual Machine;OSSkuId=48;FlightContent=Branch;App=WU;OEMName_Uncleaned=Microsoft Corporation;AppVer=10.0.22621.900;OSArchitecture=AMD64;SystemSKU=None;UpdateManagementGroup=2;IsFlightingEnabled=1;IsDeviceRetailDemo=0;TelemetryLevel=3;OSVersion=10.0.22621.900;DeviceFamily=Windows.Desktop;</deviceAttributes>
</GetExtendedUpdateInfo2>"""
    r = soap("GetExtendedUpdateInfo2", body, secured=True)
    # 一个更新回两条地址:主包 + 1MB 的 BlockMap cab(`Abm_….cab`,同样在 dl.delivery 上)。10-26 第一版按域名挑,
    #   挑中了 cab ⇒ 下了 1157327 字节就停、大小对不上。⇒ **按 FileDigest 精确对上** SyncUpdates 给的那个 SHA1。
    for loc in r.iterfind(".//w:FileLocation", NS):
        u, d = loc.find("w:Url", NS).text, (loc.find("w:FileDigest", NS).text if loc.find("w:FileDigest", NS) is not None else None)
        if u and digest and d == digest:
            return u
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    pid = sys.argv[1]
    want_arch = sys.argv[sys.argv.index("--arch") + 1] if "--arch" in sys.argv else None
    cat_id, pkgs = catalog(pid)
    out = []
    for (fname, digest, size), uid, rev in sync_updates(get_cookie(), cat_id):
        # 只要目录里列出的主包(按大小逐字节对上),依赖包(VCLibs 之类)不要
        full = next((k for k, v in pkgs.items() if v.get("MaxDownloadSizeInBytes") == size), None)
        if not full:
            continue
        m = re.match(r"([^_]+)_([^_]+)_([^_]+)__", full)
        arch = m.group(3) if m else ""
        if want_arch and arch != want_arch:
            continue
        url = file_url(uid, rev, digest)
        if not url:
            sys.exit(f"✗ {full}:微软没给出 SHA1={digest} 那个文件的地址")
        out.append({"name": fname, "package": full, "arch": arch, "version": m.group(2) if m else "",
                    "size": size, "sha1_b64": digest, "url": url})
    if not out:
        sys.exit(f"✗ {pid}:没解析出任何主包(目录里的包:{list(pkgs)})")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
