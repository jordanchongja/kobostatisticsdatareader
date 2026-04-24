import streamlit as st
import tempfile
import os
import io
import struct
import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import calmap
import plotly.express as px
from datetime import datetime, timedelta
from collections import defaultdict
import urllib.parse
import json

# ==============================================================================
# 1. PAGE SETUP & UI
# ==============================================================================
st.set_page_config(page_title="Kobo Reading Dashboard", page_icon="📚", layout="wide")
st.title("📚 Kobo Reading Dashboard")
st.markdown("Upload your `KoboReader.sqlite` file to instantly generate your reading statistics.")

# ==============================================================================
# 2. KOBO EXTRACTION UTILITIES 
# ==============================================================================
class KoboBinaryReader:
    def __init__(self, data_bytes):
        self.data = data_bytes

    def extract_timestamps(self):
        target = "eventTimestamps".encode('utf-16-be')
        idx = self.data.find(target)
        if idx == -1: return []
        stream = io.BytesIO(self.data[idx + len(target):])
        try:
            v_type = struct.unpack(">I", stream.read(4))[0]
            is_null = struct.unpack(">?", stream.read(1))[0]
            if v_type == 9 and not is_null: 
                count = struct.unpack(">I", stream.read(4))[0]
                timestamps = []
                for _ in range(count or 0):
                    i_type = struct.unpack(">I", stream.read(4))[0]
                    i_null = struct.unpack(">?", stream.read(1))[0]
                    if i_type == 3: 
                        timestamps.append(struct.unpack(">I", stream.read(4))[0])
                    else: stream.read(4)
                return timestamps
        except: pass
        return []

def cluster_and_scale(ts_list, target_total_sec, gap_limit=300):
    if not ts_list: return [], 1.0
    sorted_ts = sorted(list(set(ts_list)))
    
    raw_sessions = []
    if not sorted_ts: return [], 1.0
    
    start_ts = sorted_ts[0]
    last_ts = sorted_ts[0]
    pages = 1
    for i in range(1, len(sorted_ts)):
        if sorted_ts[i] - last_ts < gap_limit:
            pages += 1
        else:
            raw_sessions.append({'start': start_ts, 'end': last_ts, 'pages': pages})
            start_ts = sorted_ts[i]
            pages = 1
        last_ts = sorted_ts[i]
    raw_sessions.append({'start': start_ts, 'end': last_ts, 'pages': pages})
    
    raw_total_duration = sum([(s['end'] - s['start'] + 60) for s in raw_sessions])
    
    if raw_total_duration > 0 and target_total_sec < raw_total_duration:
        scaling_factor = 1.0 
    else:
        scaling_factor = target_total_sec / raw_total_duration if raw_total_duration > 0 else 1.0
    
    final_sessions = []
    for s in raw_sessions:
        corrected_dur_sec = ((s['end'] - s['start']) + 60) * scaling_factor
        dur_mins = max(1.0, round(corrected_dur_sec / 60, 2))
        final_sessions.append({
            'timestamp': datetime.fromtimestamp(s['start']).isoformat(),
            'duration_minutes': dur_mins,
            'pages_read': s['pages']
        })
    return final_sessions, scaling_factor

def safe_decode(val, default_val="Unknown"):
    if pd.isnull(val): return default_val
    if isinstance(val, bytes): return val.decode('utf-8', errors='ignore')
    return str(val)

def decode_kobo_text(text):
    if not isinstance(text, str) or not text: return ""
    try:
        if all(c in "0123456789abcdefABCDEF" for c in text) and len(text) > 2:
            return bytes.fromhex(text).decode('utf-8')
    except: pass
    return text 

def fix_sort_title(title):
    if not title: return title
    title = title.replace("_s ", "'s ").replace("_s", "'s")
    if ", " not in title: return title
    suffixes = [", The", ", A", ", An"]
    for suffix in suffixes:
        if title.endswith(suffix):
            main_title = title[:-len(suffix)]
            article = suffix[2:] 
            return f"{article} {main_title}".strip()
    return title

def process_sqlite_db(db_bytes):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
        tmp.write(db_bytes)
        db_path = tmp.name

    conn = sqlite3.connect(db_path)
    conn.text_factory = bytes 
    cursor = conn.cursor()

    # 1. Master Title Map 
    df_all_titles = pd.read_sql_query("SELECT ContentID, Title, Attribution FROM content WHERE ContentType = 6", conn)
    master_title_map = {}
    basename_map = {} # FIX: Create a map of just filenames to handle moved folders
    
    for cid, title in zip(df_all_titles['ContentID'], df_all_titles['Title']):
        raw_cid = urllib.parse.unquote(safe_decode(cid)).lower()
        clean_title = fix_sort_title(safe_decode(title))
        
        info = {'raw_title': safe_decode(title), 'clean_title': clean_title}
        master_title_map[raw_cid] = info
        basename_map[os.path.basename(raw_cid)] = info # Map the filename

    cursor.execute("PRAGMA table_info(content);")
    all_cols = [col[1].decode('utf-8') if isinstance(col[1], bytes) else col[1] for col in cursor.fetchall()]
    percent_col = "___PercentRead" if "___PercentRead" in all_cols else "PercentRead"

    df_metadata = pd.read_sql_query(f"""
        SELECT 
            Parent.ContentID, Parent.Title, Parent.Attribution as Author, 
            Parent.TimeSpentReading, Parent.{percent_col} as Percent,
            Parent.ReadStatus, Parent.DateLastRead,
            SUM(CASE WHEN Children.ContentType = 9 AND Children.WordCount > 0 THEN Children.WordCount ELSE 0 END) as NarrativeWordCount
        FROM content AS Parent
        LEFT JOIN content AS Children ON Children.BookID = Parent.ContentID
        WHERE Parent.ContentType = 6
        GROUP BY Parent.ContentID
    """, conn)

    golden_codes = [3, 5, 7, 8, 9, 46, 80, 1012, 1013, 1014]
    df_events = pd.read_sql_query(f"SELECT ContentID, ExtraData FROM Event WHERE EventType IN ({','.join(map(str, golden_codes))})", conn)
    df_bookmarks = pd.read_sql_query("SELECT VolumeID, Text, DateCreated FROM Bookmark WHERE Text IS NOT NULL", conn)
    
    processed_reading_analysis = {}
    
    for _, row in df_metadata.iterrows():
        cid = safe_decode(row['ContentID'], "Unknown_ID")
        title_raw = safe_decode(row['Title'])
        title_clean = fix_sort_title(title_raw) 
        author = safe_decode(row['Author'], "Unknown Author")
        system_sec = row['TimeSpentReading'] if pd.notnull(row['TimeSpentReading']) else 0
        
        # Exact ID match prevents double-counting ghost sessions
        book_ts = []
        relevant_events = df_events[df_events['ContentID'].apply(lambda x: safe_decode(x, "") == cid)]
        for blob in relevant_events['ExtraData']:
            book_ts.extend(KoboBinaryReader(blob).extract_timestamps())
            
        sessions, factor = cluster_and_scale(book_ts, system_sec)

        if not sessions and system_sec > 0:
            last_read = safe_decode(row.get('DateLastRead'))
            if last_read == "Unknown" or not last_read:
                last_read = datetime.now().isoformat()
            sessions = [{'timestamp': last_read, 'duration_minutes': round(system_sec/60, 2), 'pages_read': 0}]

        total_min = system_sec / 60
        raw_percent = row['Percent'] if pd.notnull(row['Percent']) else 0
        read_status = row['ReadStatus'] if pd.notnull(row['ReadStatus']) else 0
        safe_percent = 100 if read_status == 2 else max(0, min(100, raw_percent))
        wpm = round(((row['NarrativeWordCount'] or 0) * (safe_percent / 100.0)) / total_min, 1) if total_min > 0 else 0
        
        hls = [{"text": safe_decode(h['Text'], ""), "date": safe_decode(h['DateCreated'], "")} 
               for _, h in df_bookmarks[df_bookmarks['VolumeID'].apply(lambda x: urllib.parse.unquote(safe_decode(x, "")).lower() == urllib.parse.unquote(cid).lower())].iterrows()]

        if system_sec > 0 or hls or safe_percent > 0:
            processed_reading_analysis[cid] = {
                "metadata": {"title": title_clean, "author": author, "word_count": row['NarrativeWordCount'], "percent_complete": safe_percent},
                "metrics": {"total_minutes": round(total_min, 1), "avg_wpm": wpm, "session_count": len(sessions)},
                "sessions": sessions,
                "highlights": hls
            }

    # 3. Robust WordList Extraction (Fixes the Moved Folder issue)
    global_words = []
    try:
        df_words = pd.read_sql_query("SELECT * FROM WordList", conn)
        for _, row in df_words.iterrows():
            word = decode_kobo_text(safe_decode(row.get('Text', '')))
            if not word: continue
            
            w_vol_hex = safe_decode(row.get('VolumeId', row.get('VolumeID', '')))
            w_vol_raw = decode_kobo_text(w_vol_hex)
            w_vol_norm = urllib.parse.unquote(w_vol_raw).lower()
            w_vol_base = os.path.basename(w_vol_norm) # Extract just the filename
            
            # Match strictly against basename to ignore moved folders
            matched_info = basename_map.get(w_vol_base)
            
            b_title = matched_info['clean_title'] if matched_info else "Unknown Book"
            date_hex = safe_decode(row.get('DateAdded', row.get('DateCreated', 'Unknown')))
            w_date = decode_kobo_text(date_hex)
            
            global_words.append({"Book": b_title, "Word": word, "Date": w_date})
    except: pass

    conn.close()
    os.unlink(db_path)
    return processed_reading_analysis, global_words

def format_duration(minutes):
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h {mins}m"

def generate_raw_data_excel(db_bytes, dashboard_data):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
        tmp.write(db_bytes)
        db_path = tmp.name

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        
        # 1. Output the Full Reading Dashboard JSON as requested
        dash_list = []
        for file_uri, book_data in dashboard_data.items():
            dash_list.append({
                "File_URI": file_uri,
                "JSON_Payload": json.dumps(book_data, indent=4)
            })
        if dash_list:
            pd.DataFrame(dash_list).to_excel(writer, sheet_name="reading_dashboard", index=False)

        # 2. Output Raw Database Tables
        conn = sqlite3.connect(db_path)
        conn.text_factory = bytes
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0].decode('utf-8') for t in cursor.fetchall()]
        
        for table in tables:
            df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            for col in df.columns:
                if df[col].dtype == object:
                    def safe_convert(x):
                        if isinstance(x, bytes):
                            if b'\x00' in x: 
                                return x.hex()
                            try:
                                return x.decode('utf-8')
                            except UnicodeDecodeError:
                                return x.hex()
                        return x
                    df[col] = df[col].apply(safe_convert)
            
            sheet_name = table[:31]
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            
        conn.close()
    os.unlink(db_path)
    return output.getvalue()

# ==============================================================================
# 3. MAIN APP LOGIC
# ==============================================================================
if "data_processed" not in st.session_state:
    st.session_state.data_processed = False

uploaded_file = st.file_uploader("Upload your KoboReader.sqlite file", type=["sqlite", "sqlite3"])

if uploaded_file is not None:
    if not st.session_state.data_processed or st.session_state.get('last_file') != uploaded_file.name:
        st.session_state.db_bytes = uploaded_file.getvalue()
        with st.spinner("Extracting reading telemetry..."):
            dash, words = process_sqlite_db(st.session_state.db_bytes)
            st.session_state.dashboard_data = dash
            st.session_state.global_words = words
            st.session_state.last_file = uploaded_file.name
            st.session_state.data_processed = True
            
    if not st.session_state.dashboard_data:
        st.warning("No reading history found in the database.")
        st.stop()

    all_sessions, library_list, all_highlights = [], [], []
    daily_read_time, time_of_day_durations = defaultdict(float), defaultdict(float)

    for item in st.session_state.dashboard_data.values():
        meta = item.get('metadata', {})
        metrics = item.get('metrics', {})
        sessions = item.get('sessions', [])
        
        title = meta.get('title', 'Unknown')
        author = meta.get('author', 'Unknown')
        pct = meta.get('percent_complete', 0)
        
        status = "Finished" if pct >= 100 else ("Reading" if pct > 0 else "Unread")
        library_list.append({
            "Title": title,
            "Author": author,
            "Status": status,
            "Progress (%)": round(pct, 1),
            "Avg WPM": metrics.get('avg_wpm', 0),
            "Total Time (mins)": metrics.get('total_minutes', 0)
        })
        
        for h in item.get('highlights', []):
            all_highlights.append({"Book": title, "Author": author, "Highlight": h['text'], "Date": h['date'][:10]})

        for session in sessions:
            try:
                dt = datetime.fromisoformat(session['timestamp'])
            except ValueError:
                continue
                
            dur_mins = session.get('duration_minutes', 0)
            all_sessions.append({
                "Date": dt,
                "Duration": dur_mins,
                "Book": title,
                "Author": author
            })
            
            daily_read_time[dt.date()] += dur_mins
            
            hour = dt.hour
            if 5 <= hour < 12: period = 'Morning (5am-12pm)'
            elif 12 <= hour < 17: period = 'Afternoon (12pm-5pm)'
            elif 17 <= hour < 21: period = 'Evening (5pm-9pm)'
            else: period = 'Night (9pm-5am)'
            time_of_day_durations[period] += dur_mins

    # Calculate Streaks
    sorted_dates = sorted(daily_read_time.keys())
    longest_streak = current_streak = 0
    if sorted_dates:
        longest_streak = current_streak = 1
        for i in range(1, len(sorted_dates)):
            if sorted_dates[i] == sorted_dates[i-1] + timedelta(days=1):
                current_streak += 1
            else:
                longest_streak = max(longest_streak, current_streak)
                current_streak = 1
        longest_streak = max(longest_streak, current_streak)
        if (datetime.now().date() - sorted_dates[-1]).days > 1:
            current_streak = 0

    df_sessions = pd.DataFrame(all_sessions)
    if not df_sessions.empty:
        df_sessions['Date'] = pd.to_datetime(df_sessions['Date'])
        
    df_library = pd.DataFrame(library_list)
    df_highlights = pd.DataFrame(all_highlights)
    df_words = pd.DataFrame(st.session_state.global_words)
    if not df_words.empty and 'Date' in df_words.columns:
        df_words['Date'] = df_words['Date'].str[:10] 

    tabs = st.tabs(["📊 Overview", "🗓️ Calendar & Streaks", "⏱️ Time of Day", "📚 Library", "📝 Notes", "🔤 Vocab", "📈 Insights", "📥 Raw Data"])

    with tabs[0]:
        st.header("Reading Overview")
        
        if not df_sessions.empty:
            col_y, col_m = st.columns(2)
            years = ["All Time"] + sorted(df_sessions['Date'].dt.year.unique().tolist(), reverse=True)
            selected_year = col_y.selectbox("Filter by Year", years)
            
            if selected_year != "All Time":
                available_months = sorted(df_sessions[df_sessions['Date'].dt.year == selected_year]['Date'].dt.month.unique().tolist())
                month_options = ["All Year"] + [datetime(2000, m, 1).strftime('%B') for m in available_months]
                selected_month_str = col_m.selectbox("Filter by Month", month_options)
                
                filtered_sessions = df_sessions[df_sessions['Date'].dt.year == selected_year]
                if selected_month_str != "All Year":
                    month_num = datetime.strptime(selected_month_str, '%B').month
                    filtered_sessions = filtered_sessions[filtered_sessions['Date'].dt.month == month_num]
                
                period_duration = filtered_sessions['Duration'].sum()
                period_books = filtered_sessions['Book'].nunique()
            else:
                filtered_sessions = df_sessions
                period_duration = df_library['Total Time (mins)'].sum()
                period_books = df_library[df_library['Total Time (mins)'] > 0]['Title'].nunique()
                
            st.divider()
            col1, col2 = st.columns(2)
            col1.metric("Time Read in Selected Period", format_duration(period_duration))
            col2.metric("Books Interacted With", period_books)
            
            if not filtered_sessions.empty:
                st.markdown("### Books Read During This Period")
                books_in_period = filtered_sessions.groupby(['Book', 'Author'])['Duration'].sum().reset_index()
                books_in_period['Total Time Read'] = books_in_period['Duration'].apply(format_duration)
                st.dataframe(books_in_period[['Book', 'Author', 'Total Time Read']], use_container_width=True, hide_index=True)
        else:
            st.info("No reading sessions found to generate overview.")

    with tabs[1]:
        st.header("Streaks & Heatmap")
        scol1, scol2 = st.columns(2)
        scol1.metric("🔥 Longest Streak", f"{longest_streak} Days")
        scol2.metric("🚀 Current Streak", f"{current_streak} Days")
        
        if daily_read_time:
            all_days = pd.Series(daily_read_time)
            all_days.index = pd.to_datetime(all_days.index)
            all_days = all_days[all_days > 0]
            
            available_years = sorted(list(set(all_days.index.year)), reverse=True)
            target_year = st.selectbox("Select Year for Heatmap", available_years)
            
            fig, ax = plt.subplots(figsize=(10, 2.5), dpi=200)
            try:
                calmap.yearplot(all_days, year=target_year, cmap='Greens', ax=ax, linewidth=1.5)
                st.pyplot(fig, use_container_width=False)
                st.markdown("*Lighter green = Less reading time. Darker green = More reading time.*")
            except KeyError:
                st.error("No data available to plot the heatmap for this year.")
        else:
            st.info("Not enough data to generate a heatmap.")

    with tabs[2]:
        st.header("When Do You Read?")
        ordered_periods = ['Morning (5am-12pm)', 'Afternoon (12pm-5pm)', 'Evening (5pm-9pm)', 'Night (9pm-5am)']
        sorted_durs = {p: time_of_day_durations.get(p, 0) / 60 for p in ordered_periods}
        
        if any(sorted_durs.values()):
            df_tod = pd.DataFrame(list(sorted_durs.items()), columns=['Time of Day', 'Hours Read'])
            fig = px.bar(df_tod, x='Time of Day', y='Hours Read', color='Time of Day', 
                         color_discrete_sequence=['#87CEEB', '#FFA500', '#FA8072', '#00008B'])
            fig.update_layout(showlegend=False, xaxis_title="", yaxis_title="Total Hours Read")
            st.plotly_chart(fig, use_container_width=True)

    with tabs[3]:
        st.header("Interactive Library")
        st.dataframe(df_library, use_container_width=True, hide_index=True)

    with tabs[4]:
        st.header("Saved Highlights")
        if not df_highlights.empty:
            st.dataframe(df_highlights, use_container_width=True, hide_index=True)
        else:
            st.info("No highlights found.")

    with tabs[5]:
        st.header("Vocabulary Builder")
        if not df_words.empty:
            st.dataframe(df_words, use_container_width=True, hide_index=True)
        else:
            st.info("No saved vocabulary words found.")

    with tabs[6]:
        st.header("Reading Insights")
        if not df_library.empty and df_library['Avg WPM'].sum() > 0:
            valid_wpm = df_library[df_library['Avg WPM'] > 0]
            fig = px.scatter(valid_wpm, x="Total Time (mins)", y="Avg WPM", color="Status", 
                             hover_data=["Title"], title="Reading Speed vs. Time Spent")
            st.plotly_chart(fig, use_container_width=True)

    with tabs[7]:
        st.header("Raw Database Export")
        st.markdown("Download all 30+ raw Kobo tables. Includes a **reading_dashboard** sheet with full JSON structure.")
        
        if st.button("Generate Excel File"):
            with st.spinner("Compiling database tables... (This may take a moment)"):
                excel_data = generate_raw_data_excel(st.session_state.db_bytes, st.session_state.dashboard_data)
                
                st.download_button(
                    label="📥 Download Data (.xlsx)",
                    data=excel_data,
                    file_name=f"kobo_raw_data_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )