import sys
import json
import os
import requests
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QDialog, QLineEdit, QFrame, 
                             QScrollArea, QSizeGrip)
from PyQt5.QtCore import Qt, QPoint, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QCursor

# ─── 색상 테마 ────────────────────────────────────────────────────────────────
BG_COLOR        = "#0D0F14"   
HEADER_COLOR    = "#1C2130"   
BORDER_COLOR    = "#252C3D"   
TEXT_PRIMARY    = "#E8EAF2"   
TEXT_MUTED      = "#7B84A0"   
COLOR_PROFIT    = "#FF6B6B"   
COLOR_LOSS      = "#4ECDC4"   
COLOR_NEUTRAL   = "#7B84A0"   

CONFIG_FILE = "config.json"
TOSS_BASE_URL = "https://openapi.tossinvest.com"

# ─── 유틸리티 함수 ──────────────────────────────────────────────────────────
def format_money(amount):
    try:
        n = float(amount)
        return f"{int(n):,}원"
    except Exception:
        return "—"

def get_color(rate):
    if rate > 0: return COLOR_PROFIT
    elif rate < 0: return COLOR_LOSS
    return COLOR_NEUTRAL

# ─── 비동기 API 통신 스레드 ───────────────────────────────────────────────────
class ApiWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, cid, csec):
        super().__init__()
        self.cid = cid
        self.csec = csec
        self.token = None
        self.account_seq = None

    def run(self):
        try:
            # 1. 토큰 발급
            if not self.token:
                auth_resp = requests.post(
                    f"{TOSS_BASE_URL}/oauth2/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.cid,
                        "client_secret": self.csec
                    },
                    timeout=10
                )
                auth_resp.raise_for_status()
                self.token = auth_resp.json().get("access_token")

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }

            # 2. 계좌 정보 조회
            if not self.account_seq:
                acc_resp = requests.get(f"{TOSS_BASE_URL}/api/v1/accounts", headers=headers, timeout=10)
                acc_resp.raise_for_status()
                acc_data = acc_resp.json().get("result", [])
                if not acc_data:
                    raise Exception("조회 가능한 토스 계좌가 존재하지 않습니다.")
                self.account_seq = acc_data[0].get("accountSeq")

            headers["X-Tossinvest-Account"] = str(self.account_seq)

            # 3. 환율 및 보유 주식 정보 동시 조회
            fx_resp = requests.get(f"{TOSS_BASE_URL}/api/v1/exchange-rate?baseCurrency=USD&quoteCurrency=KRW", headers=headers, timeout=10)
            fx_rate = float(fx_resp.json().get("result", {}).get("rate", 0)) if fx_resp.status_code == 200 else 0

            hold_resp = requests.get(f"{TOSS_BASE_URL}/api/v1/holdings", headers=headers, timeout=10)
            hold_resp.raise_for_status()
            
            # 4. 연산 및 데이터 추출
            ov = hold_resp.json().get("result", {})
            
            total_krw = float(ov.get("marketValue", {}).get("amount", {}).get("krw", 0))
            total_usd = float(ov.get("marketValue", {}).get("amount", {}).get("usd", 0))
            pl_krw = float(ov.get("profitLoss", {}).get("amount", {}).get("krw", 0))
            pl_usd = float(ov.get("profitLoss", {}).get("amount", {}).get("usd", 0))
            
            day_krw = float(ov.get("dailyProfitLoss", {}).get("amount", {}).get("krw", 0))
            day_usd = float(ov.get("dailyProfitLoss", {}).get("amount", {}).get("usd", 0))
            
            purch_krw = float(ov.get("totalPurchaseAmount", {}).get("krw", 0))
            purch_usd = float(ov.get("totalPurchaseAmount", {}).get("usd", 0))

            total_asset = total_krw + (total_usd * fx_rate)
            total_profit = pl_krw + (pl_usd * fx_rate)
            total_rate = float(ov.get("profitLoss", {}).get("rate", 0))
            daily_pl = day_krw + (day_usd * fx_rate)
            total_purch = purch_krw + (purch_usd * fx_rate)

            items = []
            for item in ov.get("items", []):
                val = float(item.get("marketValue", {}).get("amount", 0))
                items.append({
                    "name": item.get("name") or item.get("symbol"),
                    "symbol": item.get("symbol", ""),
                    "rate": float(item.get("profitLoss", {}).get("rate", 0)),
                    "value": f"₩{val:,.0f}" if item.get("currency") == "KRW" else f"${val:,.2f}"
                })

            self.finished.emit({
                "total_asset": total_asset,
                "total_profit": total_profit,
                "total_rate": total_rate,
                "daily_pl": daily_pl,
                "total_purch": total_purch,
                "fx_rate": fx_rate,
                "items": items
            })

        except Exception as e:
            self.token = None
            self.account_seq = None
            err_msg = str(e)
            if "401" in err_msg or "403" in err_msg: 
                err_msg = "인증 세션 만료 (자동 재연결 중...)"
            self.error.emit(err_msg)

# ─── 메인 위젯 레이아웃 ───────────────────────────────────────────────────────
class TossWidget(QWidget):
    def __init__(self, cid, csec, interval):
        super().__init__()
        self.cid = cid
        self.csec = csec
        
        # [수정] 무분별하게 짧은 주기(Rate Limit) 방어를 위해 최소 15초 강제 제한
        self.interval = max(15, interval)
        self.current_interval = self.interval  # 실시간 유동적 타이머 주기 관리를 위한 변수
        
        self.old_pos = None
        self.is_collapsed = False
        self.base_opacity = 0.85

        self.initUI()
        
        self.worker = ApiWorker(self.cid, self.csec)
        self.worker.finished.connect(self.update_ui)
        self.worker.error.connect(self.show_error)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(self.current_interval * 1000)
        
        self.refresh_data()

    def initUI(self):
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(self.base_opacity)
        
        self.resize(320, 480)
        self.setMinimumSize(270, 320)

        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.setSpacing(0)

        self.bg_frame = QFrame()
        self.bg_frame.setStyleSheet(f"background-color: {BG_COLOR}; border-radius: 14px; border: 1px solid {BORDER_COLOR};")
        bg_layout = QVBoxLayout(self.bg_frame)
        bg_layout.setContentsMargins(0, 0, 0, 0)
        bg_layout.setSpacing(0)

        # 헤더
        self.header = QFrame()
        self.header.setFixedHeight(45)
        self.header.setStyleSheet(f"background-color: {HEADER_COLOR}; border-top-left-radius: 12px; border-top-right-radius: 12px; border-bottom: 1px solid {BORDER_COLOR};")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(15, 0, 15, 0)
        
        title_lbl = QLabel("📈 TossDash")
        title_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-weight: bold; font-size: 13px; border:none;")
        self.status_lbl = QLabel("연결 중...")
        self.status_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border:none;")
        
        header_layout.addWidget(title_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.status_lbl)
        bg_layout.addWidget(self.header)

        # 스크롤 영역
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { width: 6px; background: transparent; }
            QScrollBar::handle:vertical { background: #252C3D; border-radius: 3px; }
            QScrollBar::handle:vertical:hover { background: #3182F6; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)

        self.body_content = QWidget()
        self.body_content.setStyleSheet("background: transparent; border: none;")
        body_layout = QVBoxLayout(self.body_content)
        body_layout.setContentsMargins(15, 15, 15, 15)
        body_layout.setSpacing(12)

        # 상단 평가금액 종합 카드
        self.sum_card = QFrame()
        self.sum_card.setStyleSheet(f"background-color: {HEADER_COLOR}; border: 1px solid {BORDER_COLOR}; border-radius: 12px;")
        card_layout = QVBoxLayout(self.sum_card)
        card_layout.setContentsMargins(15, 15, 15, 15)
        card_layout.setSpacing(6)

        label_top = QLabel("총 평가금액")
        label_top.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border:none;")
        self.total_asset_lbl = QLabel("—")
        self.total_asset_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-weight: bold; font-size: 20px; border:none;")
        
        self.pl_container = QWidget()
        self.pl_container.setStyleSheet("border: none; background: transparent;")
        pl_layout = QHBoxLayout(self.pl_container)
        pl_layout.setContentsMargins(0, 0, 0, 0)
        pl_layout.setSpacing(8)
        
        self.pl_amt_lbl = QLabel("—")
        self.pl_amt_lbl.setStyleSheet("font-weight: bold; font-size: 12px; border:none;")
        self.pl_rate_lbl = QLabel("—")
        self.pl_rate_lbl.setStyleSheet("font-weight: bold; font-size: 12px; border:none;")
        pl_layout.addWidget(self.pl_amt_lbl)
        pl_layout.addWidget(self.pl_rate_lbl)
        pl_layout.addStretch()

        card_layout.addWidget(label_top)
        card_layout.addWidget(self.total_asset_lbl)
        card_layout.addWidget(self.pl_container)

        # 서브 메타 정보 그리드
        self.meta_frame = QFrame()
        self.meta_frame.setStyleSheet(f"border-top: 1px solid {BORDER_COLOR}; border-radius: 0px; margin-top: 5px; padding-top: 5px;")
        meta_layout = QHBoxLayout(self.meta_frame)
        meta_layout.setContentsMargins(0, 5, 0, 0)
        
        def create_meta_cell(title):
            cell = QVBoxLayout()
            t_lbl = QLabel(title)
            t_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; border:none;")
            v_lbl = QLabel("—")
            v_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 600; border:none;")
            cell.addWidget(t_lbl)
            cell.addWidget(v_lbl)
            return cell, v_lbl

        cell_purch, self.purch_lbl = create_meta_cell("매입금액")
        cell_daily, self.daily_lbl = create_meta_cell("일간 손익")
        cell_fx, self.fx_lbl = create_meta_cell("USD 환율")

        meta_layout.addLayout(cell_purch)
        meta_layout.addLayout(cell_daily)
        meta_layout.addLayout(cell_fx)
        card_layout.addWidget(self.meta_frame)
        
        body_layout.addWidget(self.sum_card)

        # 보유 종목 전체 컨테이너
        self.list_container = QWidget()
        self.list_container.setStyleSheet("background: transparent; border: none;")
        list_container_layout = QVBoxLayout(self.list_container)
        list_container_layout.setContentsMargins(0, 0, 0, 0)
        list_container_layout.setSpacing(12)

        # 보유 종목 타이틀 헤더
        list_header = QHBoxLayout()
        list_title = QLabel("보유 종목")
        list_title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; font-weight: bold; text-transform: uppercase;")
        self.cnt_lbl = QLabel("0개")
        self.cnt_lbl.setStyleSheet(f"color: {TEXT_MUTED}; background-color: {HEADER_COLOR}; padding: 2px 6px; border-radius: 6px; font-size: 10px;")
        list_header.addWidget(list_title)
        list_header.addStretch()
        list_header.addWidget(self.cnt_lbl)
        list_container_layout.addLayout(list_header)

        # 개별 종목 컨테이너
        self.items_layout = QVBoxLayout()
        self.items_layout.setSpacing(4) 
        list_container_layout.addLayout(self.items_layout)
        list_container_layout.addStretch()

        body_layout.addWidget(self.list_container)

        self.scroll.setWidget(self.body_content)
        bg_layout.addWidget(self.scroll)

        # 푸터 컨트롤 바
        self.footer = QFrame()
        self.footer.setFixedHeight(35)
        self.footer.setStyleSheet("border:none; background: transparent;")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(15, 0, 6, 0)
        
        close_btn = QPushButton("위젯 종료")
        close_btn.setStyleSheet(f"background-color: #2A1A1A; color: {COLOR_PROFIT}; border: 1px solid #4A2222; padding: 4px 10px; border-radius: 6px; font-size: 11px;")
        close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        
        github_lbl = QLabel('<a href="https://github.com/no2-J/TossDash" style="color: #7B84A0; text-decoration: none;">by no2-J</a>')
        github_lbl.setOpenExternalLinks(True)  
        github_lbl.setStyleSheet("font-size: 11px; border: none; background: transparent;")
        github_lbl.setCursor(QCursor(Qt.PointingHandCursor))
        
        self.sizegrip = QSizeGrip(self)
        self.sizegrip.setStyleSheet("width: 14px; height: 14px; background: transparent;")

        footer_layout.addWidget(close_btn)
        footer_layout.addStretch()
        footer_layout.addWidget(github_lbl) 
        footer_layout.addSpacing(4)
        footer_layout.addWidget(self.sizegrip)
        bg_layout.addWidget(self.footer)

        window_layout.addWidget(self.bg_frame)

        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - self.width() - 40, screen.height() - self.height() - 80)

    def refresh_data(self):
        self.status_lbl.setText("갱신 중...")
        if not self.worker.isRunning():
            self.worker.start()

    def update_ui(self, data):
        # [수정] 통신이 성공하면 에러 상태로 인해 늘어났던 주기(백오프)를 원래 기본 주기로 신속히 복구
        if self.current_interval != self.interval:
            self.current_interval = self.interval
            self.timer.start(self.current_interval * 1000)

        self.status_lbl.setText("정상")
        self.total_asset_lbl.setText(format_money(data['total_asset']))
        
        up = data['total_profit'] >= 0
        amt_str = ("▲ +" if up else "▼ ") + format_money(data['total_profit'])
        rate_str = f"({'+' if up else ''}{data['total_rate']*100:.2f}%)"
        color = get_color(data['total_rate'])

        self.pl_amt_lbl.setText(amt_str)
        self.pl_amt_lbl.setStyleSheet(f"color: {color}; border:none;")
        self.pl_rate_lbl.setText(rate_str)
        self.pl_rate_lbl.setStyleSheet(f"color: {color}; border:none;")

        self.purch_lbl.setText(format_money(data['total_purch']))
        
        day_up = data['daily_pl'] >= 0
        self.daily_lbl.setText(('+' if day_up else '') + format_money(data['daily_pl']))
        self.daily_lbl.setStyleSheet(f"color: {get_color(data['daily_pl'])}; font-size: 11px; font-weight:600;")
        
        self.fx_lbl.setText(f"{data['fx_rate']:,.1f}원" if data['fx_rate'] > 0 else "—")

        self.cnt_lbl.setText(f"{len(data['items'])}개")
        for i in reversed(range(self.items_layout.count())): 
            w = self.items_layout.itemAt(i).widget()
            if w: w.setParent(None)

        for item in data['items']:
            row = QFrame()
            row.setStyleSheet(f"background-color: {HEADER_COLOR}; border: 1px solid {BORDER_COLOR}; border-radius: 6px; padding: 0px;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 4, 10, 4)
            
            txt_box = QVBoxLayout()
            txt_box.setContentsMargins(0, 0, 0, 0)
            txt_box.setSpacing(1)  
            
            name_lbl = QLabel(item['name'])
            name_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: bold; border:none; background: transparent;")
            sub_lbl = QLabel(item['symbol'])
            sub_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; border:none; background: transparent;")
            txt_box.addWidget(name_lbl)
            txt_box.addWidget(sub_lbl)
            
            val_box = QVBoxLayout()
            val_box.setContentsMargins(0, 0, 0, 0)
            val_box.setSpacing(1)  
            
            val_lbl = QLabel(item['value'])
            val_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: bold; border:none; background: transparent;")
            val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            
            rate_color = get_color(item['rate'])
            rate_prefix = "▲ +" if item['rate'] > 0 else "▼ " if item['rate'] < 0 else ""
            rate_lbl = QLabel(f"{rate_prefix}{abs(item['rate']*100):.2f}%")
            rate_lbl.setStyleSheet(f"color: {rate_color}; font-size: 10px; font-weight: bold; border:none; background: transparent;")
            rate_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val_box.addWidget(val_lbl)
            val_box.addWidget(rate_lbl)

            row_layout.addLayout(txt_box)
            row_layout.addStretch()
            row_layout.addLayout(val_box)
            self.items_layout.addWidget(row)

    def show_error(self, err_msg):
        # [수정] 백오프 로직 적용: 오류 지속 발생 시 트래픽 폭탄 방지를 위해 주기를 2배씩 연장 (최대 300초 = 5분)
        self.current_interval = min(self.current_interval * 2, 300)
        self.timer.start(self.current_interval * 1000)

        self.status_lbl.setText(f"에러 (재시도: {self.current_interval}초 뒤)")
        self.total_asset_lbl.setText("연결 실패")
        self.pl_amt_lbl.setText(err_msg)
        self.pl_amt_lbl.setStyleSheet(f"color: {COLOR_PROFIT}; font-size: 11px; border:none;")
        self.pl_rate_lbl.setText("")

    # ─── 마우스 이벤트 제어 ──────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            clicked_widget = self.childAt(event.pos())
            is_header_clicked = not self.is_collapsed and self.header.geometry().contains(event.pos())
            
            is_sum_card_clicked = False
            p = clicked_widget
            while p:
                if p == self.sum_card:
                    is_sum_card_clicked = True
                    break
                p = p.parentWidget()
                
            if is_header_clicked or is_sum_card_clicked:
                self.old_pos = event.globalPos()

    def mouseMoveEvent(self, event):
        if self.old_pos:
            delta = QPoint(event.globalPos() - self.old_pos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPos()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.old_pos = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            clicked_widget = self.childAt(event.pos())
            is_header_clicked = not self.is_collapsed and self.header.geometry().contains(event.pos())
            
            is_sum_card_clicked = False
            p = clicked_widget
            while p:
                if p == self.sum_card:
                    is_sum_card_clicked = True
                    break
                p = p.parentWidget()
                
            if is_header_clicked or is_sum_card_clicked:
                self.is_collapsed = not self.is_collapsed
                
                self.header.setVisible(not self.is_collapsed)
                self.footer.setVisible(not self.is_collapsed)
                self.list_container.setVisible(not self.is_collapsed)
                self.pl_container.setVisible(not self.is_collapsed)
                self.meta_frame.setVisible(not self.is_collapsed)
                
                if self.is_collapsed:
                    self.setMinimumSize(250, 60)
                    self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                    self.sizegrip.setVisible(False)
                    self.body_content.layout().setContentsMargins(10, 10, 10, 10)
                    self.sum_card.layout().setContentsMargins(12, 12, 12, 12)
                    self.resize(self.width(), 95)
                else:
                    self.setMinimumSize(270, 320)
                    self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
                    self.sizegrip.setVisible(True)
                    self.body_content.layout().setContentsMargins(15, 15, 15, 15)
                    self.sum_card.layout().setContentsMargins(15, 15, 15, 15)
                    self.resize(self.width(), 480)
            
    def enterEvent(self, event):
        self.setWindowOpacity(1.0)

    def leaveEvent(self, event):
        self.setWindowOpacity(self.base_opacity)

# ─── 설정 대화상자 ───────────────────────────────────────────────────────────
class SetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TossDash 위젯 인증")
        self.setFixedSize(340, 190)  # [수정] 주기 입력란 제거에 따라 컴팩트하게 UI 높이 조정 (260 -> 190)
        self.setStyleSheet(f"background-color: {BG_COLOR}; color: {TEXT_PRIMARY};")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 15, 20, 20)
        layout.setSpacing(8)
        
        input_style = f"""
            background-color: {HEADER_COLOR}; 
            border: 1px solid {BORDER_COLOR}; 
            color: {TEXT_PRIMARY}; 
            padding: 8px 10px; 
            border-radius: 6px;
            font-size: 12px;
        """
        
        self.cid_input = QLineEdit()
        self.cid_input.setPlaceholderText("API Key 입력")
        self.cid_input.setStyleSheet(input_style)
        
        self.csec_input = QLineEdit()
        self.csec_input.setPlaceholderText("Secret Key 입력")
        self.csec_input.setEchoMode(QLineEdit.Password)
        self.csec_input.setStyleSheet(input_style)

        save_btn = QPushButton("설정 저장 후 시작")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #3182F6; 
                color: white; 
                padding: 7px 0px; 
                font-weight: bold; 
                font-size: 13px;
                border-radius: 8px; 
                border: none;
            }
            QPushButton:hover {
                background-color: #1b64da;
            }
        """)
        save_btn.setCursor(QCursor(Qt.PointingHandCursor))
        save_btn.clicked.connect(self.accept)

        label_style = f"color: {TEXT_MUTED}; font-size: 11px; font-weight: bold;"
        
        lbl_id = QLabel("API Key")
        lbl_id.setStyleSheet(label_style)
        
        lbl_sec = QLabel("Secret Key")
        lbl_sec.setStyleSheet(label_style)

        layout.addWidget(lbl_id)
        layout.addWidget(self.cid_input)
        layout.addSpacing(2)
        
        layout.addWidget(lbl_sec)
        layout.addWidget(self.csec_input)
        
        # [수정] 새로고침 주기 입력부 위젯 및 라벨 원천 삭제 완료
        layout.addSpacing(12)
        layout.addWidget(save_btn)

    def get_data(self):
        # [수정] 입력칸 삭제에 맞춰 안전하고 표준적인 권장 주기인 30초를 기본값으로 반환
        return self.cid_input.text().strip(), self.csec_input.text().strip(), 30

# ─── 메인 엔트리 포인트 ───────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Malgun Gothic", 9))

    app.setQuitOnLastWindowClosed(False)

    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}  

    cid = cfg.get("api_key", "")
    csec = cfg.get("secret_key", "")
    interval = cfg.get("refresh_interval_sec", 30)

    if not cid or not csec or "--setup" in sys.argv:
        dialog = SetupDialog()
        if dialog.exec_() == QDialog.Accepted:
            cid, csec, interval = dialog.get_data()
            if cid and csec:
                try:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump({"api_key": cid, "secret_key": csec, "refresh_interval_sec": interval}, f, ensure_ascii=False)
                except Exception as e:
                    print(f"설정 저장 실패: {e}")
            else: sys.exit()
        else: sys.exit()

    app.setQuitOnLastWindowClosed(True)

    widget = TossWidget(cid, csec, interval)
    widget.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
