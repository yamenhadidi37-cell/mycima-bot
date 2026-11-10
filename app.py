import time
import re
import json
from datetime import datetime
import streamlit as st
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="مستخرج MyCima فائق السرعة وقاعدة البيانات", layout="wide")

# إعدادات TMDB و Firestore
TMDB_API_KEY = "c714ec95383c51abcde6afdf2e1571b9"
FIREBASE_PROJECT_ID = "kpro-a1c5d"
FIRESTORE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/vip_media"

# --- 1. دوال استخراج وتنظيف اسم الحلقة ---
def clean_title(title):
    if not title:
        return "عمل بدون اسم"
    title = re.sub(r'مشاهدة\s+(فيلم|مسلسل|أنمي|برنامج|عرض)?', '', title)
    title = re.sub(r'\(\d{4}\)', '', title)
    title = re.sub(r'\b(HD|HQ|720p|1080p|4K|اون لاين|مدبلج|مترجم|Bluray|WEB-DL)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'[-–—|:]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title if title else "عمل بدون اسم"

def extract_clean_episode_title(raw_title: str) -> str:
    """يستخرج فقط 'الحلقة X' من العنوان الطويل"""
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
    if any(url_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.css', '.js', '.gif', '.svg', '.ico', '.woff', '.ttf']):
        return False
    ignored_domains = [
        'google', 'facebook', 'twitter', 'ads', 'analytics', 'googlesyndication', 
        'histats', 'crwdcntrl', 'doubleclick', 'adnxs', 'scorecardresearch', 'criteo'
    ]
    if any(domain in url_lower for domain in ignored_domains):
        return False
        
    keywords = [
        'embed', 'player', 'liiivideo', 'vipserver', 'vidnode', 'moshsh', 'iframe', 'vidoza', 'dood', 'streamtape'
    ]
    return any(kw in url_lower for kw in keywords)

# دالة ذكية لبناء رابط كل صفحة بدقة
def build_page_url(base_url, p_num):
    if "page=" in base_url:
        return re.sub(r'page=\d+', f'page={p_num}', base_url)
    elif "/page/" in base_url:
        return re.sub(r'/page/\d+/?', f'/page/{p_num}/', base_url)
    else:
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}page={p_num}"

# دالة لجلب الروابط من صفحة واحدة
def fetch_page_items(args):
    p_num, base_url_template, headers = args
    page_url = build_page_url(base_url_template, p_num)
    page_items = []
    try:
        response = requests.get(page_url, headers=headers, timeout=12)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            links = soup.find_all('a', href=True)
            for a in links:
                href = a.get('href', '')
                if any(x in href for x in ['/series/', '/movie/', 'watch.php', 'play.php']) or 'الحلقة' in href:
                    if href.startswith('/'):
                        href = "https://vid.mycima.cc" + href
                        
                    raw_title = a.get('title', '').strip()
                    if not raw_title:
                        img_tag = a.find('img')
                        if img_tag:
                            raw_title = img_tag.get('alt', '').strip()
                    if not raw_title:
                        raw_title = a.get_text(strip=True)
                        
                    # تنظيف اسم الحلقة ليكون "الحلقة X"
                    clean_ep = extract_clean_episode_title(raw_title) or extract_clean_episode_title(href)
                    if not clean_ep:
                        clean_ep = clean_title(raw_title)
                        
                    if len(href) > 20:
                        page_items.append({"url": href, "raw_title": raw_title, "clean_title": clean_ep})
    except:
        pass
    return page_items

# استخراج رابط السيرفر
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

    return {
        "id": str(idx),
        "title": item['clean_title'],
        "serverUrl": embed_url
    }

# --- 2. محرك بحث TMDB للأجزاء والمواسم والبوسترات العربية ---
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
        
        # بوسترات كل المواسم
        seasons_data = []
        for s in detail.get('seasons', []):
            s_num = s.get('season_number', 0)
            if s_num > 0 and s.get('poster_path'):
                seasons_data.append({
                    "season_number": s_num,
                    "season_name": s.get('name') or f"الموسم {s_num}",
                    "poster_url": f"https://image.tmdb.org/t/p/w500{s.get('poster_path')}"
                })
                
        # البوسترات المعربة بالعربي
        arabic_posters = []
        for img in detail.get('images', {}).get('posters', []):
            if img.get('iso_639_1') == 'ar' and img.get('file_path'):
                arabic_posters.append(f"https://image.tmdb.org/t/p/w500{img.get('file_path')}")
                
        main_poster = ""
        if best_match.get('poster_path'):
            main_poster = f"https://image.tmdb.org/t/p/w500{best_match.get('poster_path')}"
            
        return {
            "title": main_title,
            "main_poster": main_poster,
            "seasons": seasons_data,
            "arabic_posters": arabic_posters
        }
    except Exception as e:
        st.error(f"خطأ في الاتصال بـ TMDB: {e}")
        return None

# --- 3. دوال الحفظ في قاعدة البيانات (Firestore) ---
def save_series_to_db(series_title, poster_url, mycima_url, is_completed, episodes_data, recheck_date=""):
    doc_data = {
        "fields": {
            "title": {"stringValue": series_title},
            "type": {"stringValue": "tv"},
            "category": {"stringValue": "general"},
            "posterUrl": {"stringValue": poster_url},
            "mycimaUrl": {"stringValue": mycima_url},
            "isCompleted": {"booleanValue": is_completed},
            "recheckDate": {"stringValue": recheck_date},
            "episodes": {
                "arrayValue": {
                    "values": [
                        {
                            "mapValue": {
                                "fields": {
                                    "title": {"stringValue": ep["title"]},
                                    "videoUrl": {"stringValue": ep["serverUrl"]}
                                }
                            }
                        } for ep in episodes_data if ep.get("serverUrl")
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
                is_comp = f.get("isCompleted", {}).get("booleanValue", True)
                recheck = f.get("recheckDate", {}).get("stringValue", "")
                raw_eps = f.get("episodes", {}).get("arrayValue", {}).get("values", [])
                eps = [e.get("mapValue", {}).get("fields", {}).get("title", {}).get("stringValue") for e in raw_eps]
                ongoing.append({
                    "id": doc_id,
                    "title": title,
                    "mycimaUrl": mycima,
                    "isCompleted": is_comp,
                    "recheckDate": recheck,
                    "episodesCount": len(eps),
                    "episodesList": eps,
                    "rawEpisodes": raw_eps
                })
        return ongoing
    except:
        return []

def append_new_episodes_to_db(doc_id, new_eps, current_raw_eps):
    url = f"{FIRESTORE_URL}/{doc_id}?updateMask.fieldPaths=episodes&updateMask.fieldPaths=updatedAt"
    new_values = list(current_raw_eps)
    for ep in new_eps:
        new_values.append({
            "mapValue": {
                "fields": {
                    "title": {"stringValue": ep["title"]},
                    "videoUrl": {"stringValue": ep["serverUrl"]}
                }
            }
        })
    data = {
        "fields": {
            "episodes": {"arrayValue": {"values": new_values}},
            "updatedAt": {"stringValue": datetime.now().isoformat()}
        }
    }
    return requests.patch(url, json=data, timeout=10).status_code == 200

# ==========================================
# 4. واجهة التطبيق
# ==========================================
st.title("🚀 مستخرج MyCima فائق السرعة مع تحديد نطاق الصفحات وقاعدة البيانات")

tab1, tab2 = st.tabs(["➕ سحب وإضافة مسلسل جديد (متعدد الصفحات)", "🔄 متابعة المسلسلات قيد العرض وتحديث الحلقات"])

# --- التبويب الأول: السحب المتعدد مع TMDB ---
with tab1:
    st.write("أدخل رابط المسلسل وحدد نطاق الصفحات لسحب جميع الحلقات حتى لو كانت في صفحات متعددة:")
    
    mycima_link = st.text_input("رابط ماي سيما (قسم المسلسل أو صفحة الموسم):", 
                                value="https://cima.my-cima.net/categories-4cima.php?cat=Sons-of-Anarchy-s01")
    
    # اختيار نطاق الصفحات
    col_p1, col_p2 = st.columns(2)
    with col_p1:
        start_p = st.number_input("من الصفحة رقم:", min_value=1, max_value=5000, value=1)
    with col_p2:
        end_p = st.number_input("إلى الصفحة رقم (حدد مثلاً 5 أو 10 لسحب كل الحلقات):", min_value=1, max_value=5000, value=3)
    
    col_t1, col_t2 = st.columns([2, 1])
    series_name = col_t1.text_input("اسم المسلسل (للبحث والحفظ):", value="Sons of Anarchy")
    
    if col_t2.button("🔍 بحث عن بوسترات الأجزاء والمعرب من TMDB"):
        st.session_state["tmdb_data"] = search_tmdb_poster(series_name)
        
    tmdb_data = st.session_state.get("tmdb_data")
    if tmdb_data:
        st.markdown(f"### 🎯 النتائج لعمل: **{tmdb_data['title']}**")
        
        # بوسترات كل المواسم
        if tmdb_data.get("seasons"):
            st.markdown("#### 🎬 بوسترات أجزاء ومواسم المسلسل:")
            s_cols = st.columns(min(len(tmdb_data["seasons"]), 6))
            for i, s in enumerate(tmdb_data["seasons"]):
                with s_cols[i % 6]:
                    st.image(s["poster_url"], width=120)
                    st.caption(f"**{s['season_name']}**")
                    if st.button(f"اختيار {s['season_name']} ✓", key=f"season_btn_{s['season_number']}"):
                        st.session_state["chosen_poster"] = s["poster_url"]
                        st.rerun()

        # البوسترات المعربة بالعربي
        if tmdb_data.get("arabic_posters"):
            st.markdown("#### 🇸🇦 البوسترات المعرّبة باللغة العربية:")
            ar_cols = st.columns(min(len(tmdb_data["arabic_posters"]), 4))
            for j, ar_url in enumerate(tmdb_data["arabic_posters"]):
                with ar_cols[j % 4]:
                    st.image(ar_url, width=130)
                    if st.button(f"اختيار البوستر العربي {j+1} ✓", key=f"ar_btn_{j}"):
                        st.session_state["chosen_poster"] = ar_url
                        st.rerun()

        # البوستر الرئيسي
        if tmdb_data.get("main_poster") and not tmdb_data.get("arabic_posters"):
            st.markdown("#### 🌟 البوستر الرئيسي:")
            st.image(tmdb_data["main_poster"], width=130)
            if st.button("اختيار البوستر الرئيسي ✓", key="main_poster_btn"):
                st.session_state["chosen_poster"] = tmdb_data["main_poster"]
                st.rerun()

    final_poster = st.text_input("رابط البوستر المعتمد (المختار):", value=st.session_state.get("chosen_poster", ""))

    col_s1, col_s2 = st.columns(2)
    is_completed_choice = col_s1.selectbox("حالة المسلسل:", ["مكتمل (جميع الحلقات انتهت)", "غير مكتمل / مستمر (يحتاج سحب الحلقات الجديدة مستقبلاً)"])
    is_comp_bool = "مكتمل" in is_completed_choice
    
    recheck_time = ""
    if not is_comp_bool:
        recheck_time = col_s2.date_input("تاريخ إعادة السحب القادم للحلقات الجديدة:").strftime("%Y-%m-%d")

    # زر بدء السحب متعدد الصفحات
    if st.button("🚀 سحب جميع الحلقات من كل الصفحات وتنظيفها الآن ⚡", type="primary", use_container_width=True):
        if not mycima_link.strip():
            st.warning("يرجى إدخال الرابط أولاً.")
        elif start_p > end_p:
            st.error("رقم صفحة البداية يجب أن يكون أقل من أو يساوي صفحة النهاية!")
        else:
            status_text = st.empty()
            progress_bar = st.progress(0)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://vid.mycima.cc/"
            }
            
            status_text.text(f"1. جاري جمع الحلقات من الصفحة {start_p} إلى {end_p} بالتوازي...")
            
            # جمع الروابط من كل الصفحات المحددة بالتوازي
            all_raw_items = []
            page_args = [(p, mycima_link, headers) for p in range(start_p, end_p + 1)]
            
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(fetch_page_items, arg) for arg in page_args]
                for future in as_completed(futures):
                    res = future.result()
                    all_raw_items.extend(res)
            
            # إزالة التكرار
            unique_items = []
            seen_clean = set()
            for it in all_raw_items:
                if it["clean_title"] not in seen_clean:
                    seen_clean.add(it["clean_title"])
                    unique_items.append(it)
                    
            total_unique = len(unique_items)
            if total_unique == 0:
                st.error("لم يتم العثور على أي حلقات في هذا النطاق من الصفحات. تأكد من صحة الرابط.")
            else:
                status_text.text(f"تم العثور على {total_unique} حلقة عبر جميع الصفحات. جاري استخراج روابط السيرفرات بالتوازي...")
                
                results = []
                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(process_single_item, item, i+1, headers): item for i, item in enumerate(unique_items)}
                    completed = 0
                    for fut in as_completed(futures):
                        completed += 1
                        res = fut.result()
                        # استبعاد السيرفرات الفارغة
                        if res.get("serverUrl"):
                            results.append(res)
                        progress_bar.progress(completed / total_unique)
                        status_text.text(f"جاري معالجة الحلقة {completed} من أصل {total_unique}...")
                        
                # ترتيب الحلقات تصاعدياً: الحلقة 1، الحلقة 2...
                results.sort(key=lambda x: get_episode_number(x['title']))
                for idx, r in enumerate(results):
                    r['id'] = str(idx + 1)
                    
                st.session_state["last_scraped_episodes"] = results
                status_text.success(f"🎉 تم الانتهاء بنجاح! تم استخراج {len(results)} حلقة من جميع الصفحات بأسماء نظيفة!")

    # عرض النتائج وزر الحفظ في قاعدة البيانات
    scraped_eps = st.session_state.get("last_scraped_episodes", [])
    if scraped_eps:
        st.subheader("📋 مصفوفة الحلقات النظيفة المستخرجة (مرتبة من الأولى للأخيرة):")
        js_code = "const moviesData = [\n"
        for item in scraped_eps:
            js_code += f'    {{\n'
            js_code += f'        id: "{item["id"]}",\n'
            js_code += f'        title: "{item["title"]}",\n'  # هنا "الحلقة 1" فقط!
            js_code += f'        serverUrl: "{item["serverUrl"]}"\n'
            js_code += f'    }},\n'
        js_code += "];"
        
        st.code(js_code, language="javascript")
        
        if st.button("💾 حفظ المسلسل وجميع الحلقات مباشرة في قاعدة بيانات التطبيق", type="primary", use_container_width=True):
            with st.spinner("جاري الحفظ في Firestore..."):
                ok = save_series_to_db(series_name, final_poster, mycima_link, is_comp_bool, scraped_eps, recheck_time)
                if ok:
                    st.success(f"✅ تم حفظ مسلسل '{series_name}' و {len(scraped_eps)} حلقة نظيفة بنجاح في قاعدة البيانات!")
                else:
                    st.error("حدث خطأ أثناء الحفظ في قاعدة البيانات.")

# --- التبويب الثاني: المسلسلات المستمرة قيد العرض ---
with tab2:
    st.subheader("🔄 المسلسلات غير المكتملة المسجلة (المربوطة بماي سيما)")
    if st.button("تحديث القائمة 🔄"):
        st.session_state["ongoing_list"] = get_ongoing_series()
        st.rerun()
        
    ongoing = st.session_state.get("ongoing_list", get_ongoing_series())
    if not ongoing:
        st.info("لا توجد مسلسلات غير مكتملة مسجلة حالياً.")
    else:
        for s in ongoing:
            if not s["isCompleted"] and s["mycimaUrl"]:
                with st.expander(f"📺 {s['title']} - الحلقات الحالية: ({s['episodesCount']} حلقة) | تاريخ السحب: {s['recheckDate']}", expanded=True):
                    st.write(f"🔗 رابط ماي سيما: {s['mycimaUrl']}")
                    if st.button(f"⚡ سحب الحلقات الجديدة لـ ({s['title']})", key=f"check_{s['id']}"):
                        with st.spinner("جاري فحص الحلقات الجديدة..."):
                            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://vid.mycima.cc/"}
                            items = fetch_page_items((1, s["mycimaUrl"], headers))
                            existing = set(s["episodesList"])
                            new_items = [it for it in items if it["clean_title"] not in existing]
                            
                            if not new_items:
                                st.info("المسلسل محدث بالكامل ولا توجد حلقات جديدة حتى الآن.")
                            else:
                                st.warning(f"تم العثور على {len(new_items)} حلقات جديدة! جاري سحب الروابط...")
                                new_resolved = []
                                with ThreadPoolExecutor(max_workers=5) as ex:
                                    futures = {ex.submit(process_single_item, it, i+1, headers): it for i, it in enumerate(new_items)}
                                    for fut in as_completed(futures):
                                        res = fut.result()
                                        if res.get("serverUrl"):
                                            new_resolved.append(res)
                                
                                new_resolved.sort(key=lambda x: get_episode_number(x['title']))
                                if new_resolved:
                                    ok = append_new_episodes_to_db(s["id"], new_resolved, s["rawEpisodes"])
                                    if ok:
                                        st.success(f"🎉 تم بنجاح إضافة {len(new_resolved)} حلقات جديدة لقاعدة البيانات!")
                                        time.sleep(1)
                                        st.rerun()