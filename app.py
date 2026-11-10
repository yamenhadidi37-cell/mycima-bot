import time
import re
import json
from datetime import datetime
import streamlit as st
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="مستخرج MyCima وقاعدة البيانات", layout="wide")

TMDB_API_KEY = "c714ec95383c51abcde6afdf2e1571b9"
FIREBASE_PROJECT_ID = "kpro-a1c5d"
FIRESTORE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/vip_media"

DAYS_ARABIC = {
    "all": "يومياً (كل يوم)",
    "Sunday": "الأحد",
    "Monday": "الإثنين",
    "Tuesday": "الثلاثاء",
    "Wednesday": "الأربعاء",
    "Thursday": "الخميس",
    "Friday": "الجمعة",
    "Saturday": "السبت"
}

def clean_title(title):
    if not title:
        return "عمل بدون اسم"
    title = re.sub(r'مشاهدة\s+(فيلم|مسلسل|أنمي|برنامج|عرض)?', '', title)
    title = re.sub(r'\(\d{4}\)', '', title)
    title = re.sub(r'\b(HD|HQ|720p|1080p|4K|اون لاين|مدبلج|مترجم|Bluray|WEB-DL)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'[-–—|:]+', ' ', title)
    return re.sub(r'\s+', ' ', title).strip()

def extract_clean_episode_title(raw_title: str) -> str:
    if not raw_title:
        return ""
    match = re.search(r'الحلقة\s*(\d+)', raw_title)
    if match:
        return f"الحلقة {match.group(1)}"
    match2 = re.search(r'حلقة\s*(\d+)', raw_title)
    if match2:
        return f"الحلقة {match2.group(1)}"
    return ""

def get_episode_number(ep_title: str) -> int:
    m = re.search(r'\d+', ep_title)
    return int(m.group(0)) if m else 9999

def is_valid_embed(url):
    if not url:
        return False
    url_lower = url.lower()
    if any(url_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.css', '.js', '.gif', '.svg', '.ico']):
        return False
    ignored = ['google', 'facebook', 'twitter', 'ads', 'analytics', 'doubleclick']
    if any(d in url_lower for d in ignored):
        return False
    return any(kw in url_lower for kw in ['embed', 'player', 'liiivideo', 'vipserver', 'iframe', 'streamtape'])

def parse_html_to_items(html_text):
    page_items = []
    soup = BeautifulSoup(html_text, 'html.parser')
    links = soup.find_all('a', href=True)
    for a in links:
        href = a.get('href', '')
        if any(x in href for x in ['/series/', '/movie/', 'watch.php', 'play.php']) or 'الحلقة' in href:
            if href.startswith('/'):
                href = "https://vid.mycima.cc" + href
            raw_title = a.get('title', '').strip() or a.get_text(strip=True)
            clean_ep = extract_clean_episode_title(raw_title) or extract_clean_episode_title(href)
            if not clean_ep:
                clean_ep = clean_title(raw_title)
            if len(href) > 20:
                page_items.append({"url": href, "raw_title": raw_title, "clean_title": clean_ep})
    return page_items

def build_page_url(base_url, p_num):
    if "page=" in base_url:
        return re.sub(r'page=\d+', f'page={p_num}', base_url)
    elif "/page/" in base_url:
        return re.sub(r'/page/\d+/?', f'/page/{p_num}/', base_url)
    else:
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}page={p_num}"

def fetch_page_items(args):
    p_num, base_url_template, headers = args
    page_url = build_page_url(base_url_template, p_num)
    try:
        response = requests.get(page_url, headers=headers, timeout=12)
        if response.status_code == 200:
            return parse_html_to_items(response.text)
        elif response.status_code == 403:
            return [{"error": "cloudflare_403"}]
    except:
        pass
    return []

def process_single_item(item, idx, headers):
    target_link = item['url']
    if 'watch.php' in target_link:
        target_link = target_link.replace('watch.php', 'play.php')
    embed_url = ""
    try:
        res = requests.get(target_link, headers=headers, timeout=8)
        if res.status_code == 200:
            item_soup = BeautifulSoup(res.text, 'html.parser')
            for el in item_soup.find_all(['iframe', 'embed', 'source', 'a', 'div', 'script']):
                for attr in ['src', 'data-src', 'data-embed', 'data-url', 'href']:
                    src = el.get(attr)
                    if src and is_valid_embed(src):
                        if src.startswith('//'):
                            src = 'https:' + src
                        embed_url = src
                        break
                if embed_url:
                    break
    except:
        pass
    return {"id": str(idx), "title": item['clean_title'], "serverUrl": embed_url}

def search_tmdb_poster(query):
    if not query.strip():
        return None
    search_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={requests.utils.quote(query)}&language=ar-SA"
    try:
        r = requests.get(search_url, timeout=10).json()
        results = r.get('results', [])
        if not results:
            search_url_en = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={requests.utils.quote(query)}"
            results = requests.get(search_url_en, timeout=10).json().get('results', [])
        if not results:
            return None
        best_match = results[0]
        tv_id = best_match.get('id')
        main_title = best_match.get('name') or best_match.get('original_name') or query
        detail_url = f"https://api.themoviedb.org/3/tv/{tv_id}?api_key={TMDB_API_KEY}&language=ar-SA&append_to_response=images&include_image_language=ar,en,null"
        detail = requests.get(detail_url, timeout=10).json()
        seasons_data = []
        for s in detail.get('seasons', []):
            s_num = s.get('season_number', 0)
            if s_num > 0 and s.get('poster_path'):
                seasons_data.append({"season_number": s_num, "season_name": s.get('name') or f"الموسم {s_num}", "poster_url": f"https://image.tmdb.org/t/p/w500{s.get('poster_path')}"})
        arabic_posters = []
        for img in detail.get('images', {}).get('posters', []):
            if img.get('iso_639_1') == 'ar' and img.get('file_path'):
                arabic_posters.append(f"https://image.tmdb.org/t/p/w500{img.get('file_path')}")
        main_poster = f"https://image.tmdb.org/t/p/w500{best_match.get('poster_path')}" if best_match.get('poster_path') else ""
        return {"title": main_title, "main_poster": main_poster, "seasons": seasons_data, "arabic_posters": arabic_posters}
    except:
        return None

def save_series_to_db(series_title, poster_url, mycima_url, is_completed, episodes_data, schedule_day="all", schedule_time="20:00"):
    doc_data = {
        "fields": {
            "title": {"stringValue": series_title},
            "type": {"stringValue": "tv"},
            "category": {"stringValue": "general"},
            "posterUrl": {"stringValue": poster_url},
            "mycimaUrl": {"stringValue": mycima_url},
            "isCompleted": {"booleanValue": bool(is_completed)},
            "scheduleDay": {"stringValue": schedule_day},
            "scheduleTime": {"stringValue": schedule_time},
            "episodes": {
                "arrayValue": {
                    "values": [
                        {"mapValue": {"fields": {"title": {"stringValue": ep["title"]}, "videoUrl": {"stringValue": ep["serverUrl"]}}}}
                        for ep in episodes_data if ep.get("serverUrl")
                    ]
                }
            },
            "createdAt": {"stringValue": datetime.now().isoformat()},
            "updatedAt": {"stringValue": datetime.now().isoformat()}
        }
    }
    try:
        res = requests.post(FIRESTORE_URL, json=doc_data, timeout=10)
        return res.status_code in [200, 201]
    except Exception as e:
        st.error(f"خطأ في الحفظ: {e}")
        return False

def get_ongoing_series():
    try:
        r = requests.get(FIRESTORE_URL, timeout=10).json()
        docs = r.get("documents", [])
        ongoing = []
        for d in docs:
            f = d.get("fields", {})
            if f.get("type", {}).get("stringValue") == "tv":
                doc_id = d["name"].split("/")[-1]
                title = f.get("title", {}).get("stringValue", "")
                mycima = f.get("mycimaUrl", {}).get("stringValue", "")
                is_comp = f.get("isCompleted", {}).get("booleanValue", False)
                day = f.get("scheduleDay", {}).get("stringValue", "all")
                sched_time = f.get("scheduleTime", {}).get("stringValue", "20:00")
                raw_eps = f.get("episodes", {}).get("arrayValue", {}).get("values", [])
                eps = [e.get("mapValue", {}).get("fields", {}).get("title", {}).get("stringValue") for e in raw_eps]
                if not is_comp:
                    ongoing.append({
                        "id": doc_id, "title": title, "mycimaUrl": mycima, "isCompleted": is_comp,
                        "scheduleDay": day, "scheduleTime": sched_time, "episodesCount": len(eps),
                        "episodesList": eps, "rawEpisodes": raw_eps
                    })
        return ongoing
    except:
        return []

def append_new_episodes_to_db(doc_id, new_eps, current_raw_eps):
    url = f"{FIRESTORE_URL}/{doc_id}?updateMask.fieldPaths=episodes&updateMask.fieldPaths=updatedAt"
    new_values = list(current_raw_eps)
    for ep in new_eps:
        new_values.append({
            "mapValue": {"fields": {"title": {"stringValue": ep["title"]}, "videoUrl": {"stringValue": ep["serverUrl"]}}}
        })
    data = {"fields": {"episodes": {"arrayValue": {"values": new_values}}, "updatedAt": {"stringValue": datetime.now().isoformat()}}}
    return requests.patch(url, json=data, timeout=10).status_code == 200

# ==========================================
# 4. واجهة التطبيق
# ==========================================
st.title("🚀 مستخرج MyCima وقاعدة البيانات")

tab1, tab2 = st.tabs(["➕ سحب وإضافة مسلسل جديد", "🔄 متابعة المسلسلات المستمرة وجلب الحلقات الناقصة"])

with tab1:
    scrape_mode = st.radio("طريقة السحب:", ["🌐 عبر رابط ماي سيما المباشر (للجهاز المحلي)", "📋 لصق كود الصفحة (View-Source) لتجاوز حظر السحابة 100%"], horizontal=True)
    
    raw_html_input = ""
    mycima_link = ""
    
    if "لصق كود الصفحة" in scrape_mode:
        st.info("💡 افتح رابط ماي سيما في متصفحك واضغط Ctrl+U (أو اكتب قبل الرابط view-source:) ثم انسخ كل الكود والصقه هنا:")
        raw_html_input = st.text_area("ألصق كود الـ HTML هنا:", height=150)
        mycima_link = st.text_input("رابط صفحة المسلسل (لحفظه في قاعدة البيانات):", value="https://cima.my-cima.net/categories-4cima.php?cat=Sons-of-Anarchy-s01")
        start_p, end_p = 1, 1
    else:
        mycima_link = st.text_input("رابط ماي سيما:", value="https://cima.my-cima.net/categories-4cima.php?cat=Sons-of-Anarchy-s01")
        col_p1, col_p2 = st.columns(2)
        start_p = col_p1.number_input("من الصفحة رقم:", min_value=1, max_value=5000, value=1)
        end_p = col_p2.number_input("إلى الصفحة رقم:", min_value=1, max_value=5000, value=3)

    col_t1, col_t2 = st.columns([2, 1])
    series_name = col_t1.text_input("اسم المسلسل:", value="Sons of Anarchy")
    if col_t2.button("🔍 بحث عن بوسترات TMDB"):
        st.session_state["tmdb_data"] = search_tmdb_poster(series_name)
        
    tmdb_data = st.session_state.get("tmdb_data")
    if tmdb_data:
        st.markdown(f"### 🎯 النتائج لعمل: **{tmdb_data['title']}**")
        if tmdb_data.get("seasons"):
            st.markdown("#### 🎬 بوسترات أجزاء ومواسم المسلسل:")
            s_cols = st.columns(min(len(tmdb_data["seasons"]), 6))
            for i, s in enumerate(tmdb_data["seasons"]):
                with s_cols[i % 6]:
                    st.image(s["poster_url"], width=120)
                    st.caption(f"**{s['season_name']}**")
                    if st.button(f"اختيار {s['season_name']} ✓", key=f"s_btn_{s['season_number']}"):
                        st.session_state["chosen_poster"] = s["poster_url"]
                        st.rerun()
        if tmdb_data.get("arabic_posters"):
            st.markdown("#### 🇸🇦 البوسترات المعرّبة بالعربي:")
            ar_cols = st.columns(min(len(tmdb_data["arabic_posters"]), 4))
            for j, ar_url in enumerate(tmdb_data["arabic_posters"]):
                with ar_cols[j % 4]:
                    st.image(ar_url, width=130)
                    if st.button(f"اختيار البوستر العربي {j+1} ✓", key=f"ar_btn_{j}"):
                        st.session_state["chosen_poster"] = ar_url
                        st.rerun()

    final_poster = st.text_input("رابط البوستر المعتمد:", value=st.session_state.get("chosen_poster", ""))
    
    col_c1, col_c2, col_c3 = st.columns(3)
    status_selection = col_c1.selectbox("حالة المسلسل:", ["غير مكتمل / قيد العرض (يحتاج سحب مستمر)", "مكتمل (انتهت كل حلقاته)"])
    is_completed_val = (status_selection == "مكتمل (انتهت كل حلقاته)")
    chosen_day = "all"
    chosen_time = "20:00"
    if not is_completed_val:
        chosen_day = col_c2.selectbox("يوم نزول الحلقة الجديدة:", list(DAYS_ARABIC.keys()), format_func=lambda x: DAYS_ARABIC[x])
        chosen_time = col_c3.time_input("وقت نزول الحلقة:", value=datetime.strptime("20:00", "%H:%M").time()).strftime("%H:%M")

    if st.button("🚀 سحب الحلقات وتنظيفها الآن ⚡", type="primary", use_container_width=True):
        status_text = st.empty()
        progress_bar = st.progress(0)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://vid.mycima.cc/"}
        
        all_raw_items = []
        if raw_html_input.strip():
            all_raw_items = parse_html_to_items(raw_html_input)
        else:
            page_args = [(p, mycima_link, headers) for p in range(start_p, end_p + 1)]
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(fetch_page_items, arg) for arg in page_args]
                for future in as_completed(futures):
                    res = future.result()
                    if res and res[0].get("error") == "cloudflare_403":
                        st.error("⚠️ سيرفر السحابة محظور من ماي سيما (Cloudflare 403). استخدم خيار 'لصق كود الصفحة' بالأعلى أو شغّل السكربت على حاسوبك!")
                        all_raw_items = []
                        break
                    all_raw_items.extend(res)
        
        unique_items = []
        seen_clean = set()
        for it in all_raw_items:
            if it.get("clean_title") and it["clean_title"] not in seen_clean:
                seen_clean.add(it["clean_title"])
                unique_items.append(it)
                
        if unique_items:
            results = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(process_single_item, item, i+1, headers): item for i, item in enumerate(unique_items)}
                completed = 0
                for fut in as_completed(futures):
                    completed += 1
                    res = fut.result()
                    if res.get("serverUrl"):
                        results.append(res)
                    progress_bar.progress(completed / len(unique_items))
            results.sort(key=lambda x: get_episode_number(x['title']))
            for idx, r in enumerate(results):
                r['id'] = str(idx + 1)
            st.session_state["last_scraped_episodes"] = results
            status_text.success(f"🎉 تم استخراج {len(results)} حلقة بنجاح وبأسماء نظيفة!")

    scraped_eps = st.session_state.get("last_scraped_episodes", [])
    if scraped_eps:
        st.subheader("📋 مصفوفة الحلقات النظيفة المستخرجة:")
        js_code = "const moviesData = [\n"
        for item in scraped_eps:
            js_code += f'    {{\n        id: "{item["id"]}",\n        title: "{item["title"]}",\n        serverUrl: "{item["serverUrl"]}"\n    }},\n'
        js_code += "];"
        st.code(js_code, language="javascript")
        if st.button("💾 حفظ المسلسل وجميع الحلقات في قاعدة البيانات", type="primary", use_container_width=True):
            with st.spinner("جاري الحفظ في Firestore..."):
                ok = save_series_to_db(series_name, final_poster, mycima_link, is_completed_val, scraped_eps, chosen_day, chosen_time)
                if ok:
                    st.success(f"✅ تم حفظ مسلسل '{series_name}' بنجاح وحالته: {'مكتمل' if is_completed_val else 'غير مكتمل (قيد العرض)'}!")
                else:
                    st.error("حدث خطأ أثناء الحفظ.")

with tab2:
    st.subheader("🔄 المسلسلات غير المكتملة (قيد العرض) وجدول المواعيد")
    if st.button("تحديث القائمة من قاعدة البيانات 🔄"):
        st.session_state["ongoing_list"] = get_ongoing_series()
        st.rerun()
    ongoing = st.session_state.get("ongoing_list", get_ongoing_series())
    if not ongoing:
        st.info("لا توجد مسلسلات مسجلة كـ 'غير مكتملة'.")
    else:
        for s in ongoing:
            day_label = DAYS_ARABIC.get(s["scheduleDay"], s["scheduleDay"])
            with st.expander(f"📺 {s['title']} | الحلقات الحالية: ({s['episodesCount']} حلقة) | موعد النزول: كل {day_label} الساعة {s['scheduleTime']}", expanded=True):
                st.write(f"🔗 رابط ماي سيما المربوط: {s['mycimaUrl']}")
                col_ck1, col_ck2 = st.columns([1, 3])
                check_pages = col_ck1.number_input(f"فحص حتى صفحة رقم:", min_value=1, max_value=20, value=3, key=f"p_{s['id']}")
                if col_ck2.button(f"⚡ فحص وجلب كل الحلقات الناقصة لـ ({s['title']})", key=f"btn_{s['id']}"):
                    with st.spinner(f"جاري فحص ماي سيما من صفحة 1 إلى {check_pages}..."):
                        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://vid.mycima.cc/"}
                        all_fetched = []
                        p_args = [(p, s["mycimaUrl"], headers) for p in range(1, check_pages + 1)]
                        with ThreadPoolExecutor(max_workers=5) as ex:
                            futs = [ex.submit(fetch_page_items, arg) for arg in p_args]
                            for f in as_completed(futs):
                                all_fetched.extend(f.result())
                        existing_set = set(s["episodesList"])
                        missing_items = []
                        seen_m = set()
                        for it in all_fetched:
                            if it.get("clean_title") and it["clean_title"] not in existing_set and it["clean_title"] not in seen_m:
                                seen_m.add(it["clean_title"])
                                missing_items.append(it)
                        if not missing_items:
                            st.info("المسلسل محدث تماماً!")
                        else:
                            new_resolved = []
                            with ThreadPoolExecutor(max_workers=5) as ex:
                                futures = {ex.submit(process_single_item, it, i+1, headers): it for i, it in enumerate(missing_items)}
                                for fut in as_completed(futures):
                                    res = fut.result()
                                    if res.get("serverUrl"):
                                        new_resolved.append(res)
                            new_resolved.sort(key=lambda x: get_episode_number(x['title']))
                            if new_resolved:
                                ok = append_new_episodes_to_db(s["id"], new_resolved, s["rawEpisodes"])
                                if ok:
                                    st.success(f"🎉 تم بنجاح إضافة {len(new_resolved)} حلقات ناقصة!")
                                    time.sleep(1)
                                    st.session_state["ongoing_list"] = get_ongoing_series()
                                    st.rerun()
