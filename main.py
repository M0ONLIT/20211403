import sys
import os
import time
import json
import requests
import chess
import chess.pgn
import chess.engine
import chess.svg
from functools import wraps

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, 
                             QVBoxLayout, QTextEdit, QListWidget, QLabel, QSplitter, QPushButton)
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def wait(seconds=1.0):
    """UI 갱신 부하를 줄이기 위한 쓰로틀링 데코레이터"""
    def decorator(func):
        last_called = 0.0
        @wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal last_called
            current_time = time.time()
            if current_time - last_called >= seconds:
                last_called = current_time
                return func(*args, **kwargs)
            return None
        return wrapper
    return decorator

# 1. 스톡피시 분석 스레드 (Multi-PV 및 FEN 추출)
class StockfishWorker(QThread):
    eval_ready = pyqtSignal(list, str) # (분석데이터 리스트, FEN 문자열)

    def __init__(self, fen, engine_path):
        super().__init__()
        self.fen = fen
        self.engine_path = engine_path
        self._is_running = True

    def run(self):
        try:
            engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
            board = chess.Board(self.fen)
            
            # 1. 3개의 라인을 실시간으로 추적하기 위한 버퍼
            # 인덱스 1, 2, 3번에 해당하는 최신 정보를 저장합니다.
            latest_pv_info = {1: None, 2: None, 3: None}

            # 2. engine.analysis에 multipv=3을 직접 전달
            with engine.analysis(board, chess.engine.Limit(time=20.0), multipv=3) as analysis:
                for info in analysis:
                    if not self._is_running:
                        break
                    
                    # 현재 정보가 몇 번째 후보 수순인지 확인 (1, 2, 3 중 하나)
                    rank = info.get("multipv")
                    if rank in latest_pv_info:
                        score = info.get("score")
                        pv = info.get("pv")
                        
                        if score and pv:
                            # 데이터 가공
                            score_val = score.relative.score(mate_score=10000) / 100.0
                            line_str = board.variation_san(pv[:10])
                            
                            # 해당 순위의 정보를 업데이트
                            latest_pv_info[rank] = {"score": score_val, "line": line_str}
                    
                    # 3. 버퍼에 유효한 정보가 쌓였다면 리스트로 묶어서 UI에 전송
                    # None을 제외한 실제 분석 결과만 모읍니다.
                    display_data = [v for k, v in sorted(latest_pv_info.items()) if v is not None]
                    
                    if display_data:
                        self.eval_ready.emit(display_data, self.fen)

            engine.quit()
        except Exception as e:
            print(f"Engine Error: {e}")

    def stop(self):
        self._is_running = False

# 2. 로컬 LLM 설명 스레드 (LM Studio 스트리밍)
class LLMWorker(QThread):
    chunk_ready = pyqtSignal(str) # 실시간 텍스트 조각 전송

    def __init__(self, top_lines, fen):
        super().__init__()
        self.top_lines = top_lines
        self.fen = fen
        self.api_url = "http://127.0.0.1:1234/v1/chat/completions"
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        # 엔진 분석 데이터를 텍스트로 변환
        lines_text = ""
        for i, data in enumerate(self.top_lines):
            lines_text += f"후보 {i+1}: {data['line']} (평가점수: {data['score']:+.2f})\n"

        board = chess.Board(self.fen)
        ascii_board = board.unicode(borders=True)

        system_content = (
            "당신은 전 세계 최고의 체스 전략가이자 코치입니다. "
            "제공되는 FEN(보드 상태)과 스톡피시의 상위 3개 추천 수순을 바탕으로 현재 국면을 깊이 있게 분석하세요. "
            "반드시 한국어로 답변하고, 전문 용어(중앙 통제, 기물 활동성 등)를 사용하세요. "
            "마크다운은 절대 사용하지 말고, 평어체로 500자 내외로 설명하세요."
        )
        
        user_content = (
            f"현재 보드 상태: {ascii_board}\n\n"
            f"스톡피시 추천 수순:\n{lines_text}\n"
            "이 상황에서 가장 전략적인 선택은 무엇이며, 백과 흑 중 누가 유리한지 그 이유를 설명해 주세요."
        )

        payload = {
            "model": "google/gemma-3-12b", # 사용자가 설정한 모델명
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.7,
            "max_tokens": 1000,
            "stream": True
        }

        try:
            response = requests.post(self.api_url, json=payload, stream=True, timeout=60)
            for line in response.iter_lines():
                if not self._is_running: break
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith("data: "):
                        json_str = decoded_line[6:]
                        if json_str == "[DONE]": break
                        
                        chunk_json = json.loads(json_str)
                        content = chunk_json['choices'][0]['delta'].get('content', '')
                        if content:
                            self.chunk_ready.emit(content)
        except Exception as e:
            if self._is_running:
                self.chunk_ready.emit(f"\n[AI 서버 연결 오류: {str(e)}]")

# 3. 메인 UI 클래스
class ChessAnalysisUI(QMainWindow):
    def __init__(self, pgn_path):
        super().__init__()
        # 경로 설정
        self.stockfish_path = os.path.join(BASE_DIR, "stockfish", "stockfish-windows-x86-64-avx2.exe")
        
        # 상태 및 데이터 초기화
        self.positions = []
        self.move_display_list = []
        self.current_move_idx = 0
        
        self.last_analysis_data = [] # Multi-PV 데이터 저장
        self.last_fen = ""           # 현재 FEN 저장
        self.ai_generated_for_pos = False 
        
        self.sf_worker = None
        self.llm_worker = None

        # 3초 수 읽기 타이머
        self.analysis_timer = QTimer()
        self.analysis_timer.setSingleShot(True)
        self.analysis_timer.timeout.connect(self.request_llm_explanation)

        self.load_pgn(pgn_path)
        self.init_ui()
        self.update_display()

    def load_pgn(self, path):
        """PGN 파일을 읽어 내부 보드 리스트를 구축합니다."""
        if not os.path.exists(path):
            print(f"Error: {path} 파일을 찾을 수 없습니다.")
            return

        with open(path, encoding='utf-8') as f:
            game = chess.pgn.read_game(f)
        
        temp_board = game.board()
        self.positions = [temp_board.copy()]
        self.move_display_list = ["시작 포지션"]
        
        for i, move in enumerate(game.mainline_moves()):
            move_num = (i // 2) + 1
            is_white = (i % 2 == 0)
            san_move = temp_board.san(move)
            display_text = f"{move_num}. {san_move}" if is_white else f"{move_num}... {san_move}"
            self.move_display_list.append(display_text)
            temp_board.push(move)
            self.positions.append(temp_board.copy())

    def init_ui(self):
        self.setWindowTitle("AI Chess Strategic Coach")
        self.setGeometry(100, 100, 1400, 850)
        
        main_layout = QHBoxLayout()

        # 왼쪽: 기보 리스트
        self.move_list = QListWidget()
        self.move_list.setFixedWidth(220)
        self.move_list.addItems(self.move_display_list)
        self.move_list.currentRowChanged.connect(self.on_move_clicked)
        self.move_list.setStyleSheet("font-size: 14px; background-color: #f9f9f9;")

        # 중앙: 체스판 (SVG)
        self.board_view = QSvgWidget()
        self.board_view.setFixedSize(600, 600)

        # 오른쪽: 분석 패널
        right_panel_container = QWidget()
        right_v_layout = QVBoxLayout(right_panel_container)

        self.refresh_btn = QPushButton("🔄 AI 분석 새로고침")
        self.refresh_btn.clicked.connect(self.manual_refresh_ai)
        self.refresh_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; height: 40px;")

        self.engine_output = QTextEdit()
        self.engine_output.setReadOnly(True)
        self.engine_output.setStyleSheet("font-family: Consolas; background: #2b2b2b; color: #a9b7c6; font-size: 13px;")

        self.llm_output = QTextEdit()
        self.llm_output.setReadOnly(True)
        self.llm_output.setStyleSheet("font-family: 'Malgun Gothic'; font-size: 15px; padding: 15px; line-height: 1.6;")

        right_v_layout.addWidget(QLabel("<b>📊 Stockfish Multi-PV Analysis</b>"))
        right_v_layout.addWidget(self.engine_output, 1)
        right_v_layout.addWidget(self.refresh_btn)
        right_v_layout.addWidget(QLabel("<b>🧠 Strategic Interpretation</b>"))
        right_v_layout.addWidget(self.llm_output, 2)

        main_layout.addWidget(self.move_list)
        main_layout.addWidget(self.board_view)
        main_layout.addWidget(right_panel_container)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def on_move_clicked(self, row):
        if 0 <= row < len(self.positions):
            self.current_move_idx = row
            self.update_display()

    def update_display(self):
        """포지션 이동 시 UI 및 분석 타이머 갱신"""
        board = self.positions[self.current_move_idx]
        
        # 보드 시각화
        svg_data = chess.svg.board(board).encode("utf-8")
        self.board_view.load(svg_data)
        
        # 기보 하이라이트
        self.move_list.blockSignals(True)
        self.move_list.setCurrentRow(self.current_move_idx)
        self.move_list.blockSignals(False)
        
        self.start_analysis(board)

    def start_analysis(self, board):
        # 1. 기존 워커 안전 종료
        if self.sf_worker and self.sf_worker.isRunning():
            self.sf_worker.stop()
            self.sf_worker.wait()
        
        self.analysis_timer.stop()
        self.ai_generated_for_pos = False
        
        # 2. UI 및 임시 데이터 초기화
        self.engine_output.clear()
        self.llm_output.clear()
        self.last_analysis_data = [] # 새 분석을 위해 비움
        
        self.engine_output.append("🔍 엔진이 수 읽기를 시작합니다 (3초 후 AI 분석 시작)...")
        self.llm_output.setPlaceholderText("엔진이 분석을 마칠 때까지 대기 중입니다.")

        # 3. 엔진 실행 및 3초 타이머 가동
        self.sf_worker = StockfishWorker(board.fen(), self.stockfish_path)
        self.sf_worker.eval_ready.connect(self.on_engine_update)
        self.sf_worker.start()

        self.analysis_timer.start(3000)

    @wait(seconds=1.0)
    def on_engine_update(self, analysis_data, fen):
        """실시간 엔진 결과 UI 출력"""
        self.last_analysis_data = analysis_data
        self.last_fen = fen
        
        self.engine_output.clear()
        self.engine_output.append(f"<small>FEN: {fen}</small><br>")
        for i, data in enumerate(analysis_data):
            color = "#4a90e2" if i == 0 else "#a9b7c6"
            self.engine_output.append(
                f"<font color='{color}'><b>[{i+1}] {data['score']:+.2f}</b>: {data['line']}</font>"
            )

    def request_llm_explanation(self):
        """3초 후 데이터가 준비되면 AI 분석 요청"""
        # 데이터가 아직 안 왔다면 1초 뒤 재시도
        if not self.last_analysis_data or not self.last_fen:
            QTimer.singleShot(1000, self.request_llm_explanation)
            return

        if not self.ai_generated_for_pos:
            # 기존 AI 워커 중단 및 시그널 해제 (섞임 방지)
            if self.llm_worker and self.llm_worker.isRunning():
                try: self.llm_worker.chunk_ready.disconnect()
                except: pass
                self.llm_worker.stop()
                self.llm_worker.wait()

            self.llm_output.clear()
            self.llm_output.append("🤖 <b>AI 코치 분석 중...</b>\n")
            
            self.llm_worker = LLMWorker(self.last_analysis_data, self.last_fen)
            self.llm_worker.chunk_ready.connect(self.append_llm_text)
            self.llm_output.clear()
            self.llm_worker.start()
            self.ai_generated_for_pos = True

    def manual_refresh_ai(self):
        self.ai_generated_for_pos = False
        self.request_llm_explanation()

    def append_llm_text(self, text):
        cursor = self.llm_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.llm_output.setTextCursor(cursor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Right and self.current_move_idx < len(self.positions)-1:
            self.current_move_idx += 1
            self.update_display()
        elif event.key() == Qt.Key.Key_Left and self.current_move_idx > 0:
            self.current_move_idx -= 1
            self.update_display()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    # 실행 시 프로젝트 폴더 내 example_game.pgn이 있어야 합니다.
    pgn_file = os.path.join(BASE_DIR, "example_game.pgn")
    window = ChessAnalysisUI(pgn_file)
    window.show()
    sys.exit(app.exec())
