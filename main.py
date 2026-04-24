import sys
import chess
import chess.pgn
import chess.engine
import chess.svg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, 
                             QVBoxLayout, QTextEdit, QListWidget, QLabel, QSplitter, QPushButton)
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

import requests
import json
import time
from functools import wraps

def wait(seconds=1.0):
    """함수 호출 후 지정된 시간이 지나기 전까지의 추가 호출을 무시합니다."""
    def decorator(func):
        last_called = 0.0  # 마지막으로 함수가 실행된 시간 저장
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal last_called
            current_time = time.time()
            
            # 마지막 호출로부터 지정된 시간이 지났는지 확인
            if current_time - last_called >= seconds:
                last_called = current_time
                return func(*args, **kwargs)
            
            # 시간 미달 시 아무 작업도 하지 않고 리턴
            return None
        return wrapper
    return decorator

# 1. 스톡피시 분석 스레드
class StockfishWorker(QThread):
    eval_ready = pyqtSignal(str, str) # (평가값, 베스트 라인)

    def __init__(self, fen, engine_path):
        super().__init__()
        self.fen = fen
        self.engine_path = engine_path
        self._is_running = True

    def run(self):
        try:
            engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
            board = chess.Board(self.fen)
            
            # 최대 20초간 분석 수행 (주기적으로 정보를 업데이트)
            with engine.analysis(board, chess.engine.Limit(time=20.0)) as analysis:
                # run 메서드 내부 수정 예시
                    for info in analysis:
                        if not self._is_running: break
                        
                        score = info.get("score")
                        pv = info.get("pv")
                        depth = info.get("depth", 0) # 현재 탐색 깊이 가져오기
                        
                        if score and pv:
                            # 조건: 깊이가 어느 정도(예: 10) 확보되었거나, 수순이 5수 이상일 때만 업데이트
                            if len(pv) >= 5:
                                score_str = str(score.relative.score(mate_score=10000) / 100.0)
                                # variation_san은 pv가 5개보다 적어도 에러 없이 있는 만큼만 반환합니다.
                                line_str = board.variation_san(pv[:5]) 
                                self.eval_ready.emit(score_str, line_str)
            engine.quit()
        except Exception as e:
            print(f"Engine Error: {e}")

    def stop(self):
        self._is_running = False

# 2. 로컬 LLM 설명 스레드


class LLMWorker(QThread):
    chunk_ready = pyqtSignal(str)

    def __init__(self, best_line, score):
        super().__init__()
        self.best_line = best_line
        self.score = score
        # LM Studio 기본 API 엔드포인트
        self.api_url = "http://127.0.0.1:1234/v1/chat/completions"
        self._is_running = True # 실행 상태 플래그

    def stop(self):
        """외부에서 스레드를 안전하게 멈추기 위한 함수"""
        self._is_running = False
    
    def run(self):
        # [프롬프트 엔지니어링] 체스 그랜드마스터 페르소나와 전략적 가이드라인 부여
        system_content = (
            "당신은 전 세계 최고의 체스 전략가이자 그랜드마스터 코치입니다. "
            "엔진의 수치 데이터를 기반으로 인간이 이해할 수 있는 전략적 통찰을 제공하세요. "
            "반드시 한국어로 답변하고, '폰 구조', '중앙 장악력', '기물 활동성' 등의 전문 용어를 활용하세요."
            "마크다운 형식은 쓰지 마세요."
            "500자 정도로 간단히 설명하세요."
        )
        
        user_content = (
            f"현재 스톡피시 평가값: {self.score}\n"
            f"추천하는 최선 수순(Best Line): {self.best_line}\n\n"
            "이 포지션에서 기물들이 가진 전략적 의도와 백/흑 중 누가 유리한지, 그 이유는 무엇인지 설명해 주세요."
        )

        # OpenAI 호환 규격의 메시지 페이로드
        payload = {
            "model": "google/gemma-3-12b",
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.7,
            "max_tokens": 1000,
            "stream": True
        }

        try:
            # 타임아웃을 60초로 넉넉하게 잡고 stream 모드로 연결
            response = requests.post(
                self.api_url, 
                json=payload, 
                stream=True, 
                timeout=60 
            )
            # 서버로부터 오는 조각들을 한 줄씩 읽습니다.
            for line in response.iter_lines():
                if not self._is_running:
                    break
                if line:
                    # 'data: ' 접두사 제거 후 JSON 파싱
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith("data: "):
                        json_str = decoded_line[6:]
                        if json_str == "[DONE]": break # 전송 완료
                        
                        chunk_json = json.loads(json_str)
                        content = chunk_json['choices'][0]['delta'].get('content', '')
                        
                        if content:
                            self.chunk_ready.emit(content) # UI로 한 조각 전달

        except Exception as e:
            self.chunk_ready.emit(f"\n[에러 발생: {str(e)}]")

class ChessAnalysisUI(QMainWindow):
    def __init__(self, pgn_path):
        super().__init__()
        self.stockfish_path = "./stockfish/stockfish-windows-x86-64-avx2.exe"
        
        # 데이터 초기화
        self.positions = []
        self.move_display_list = []
        self.load_pgn(pgn_path)
        
        self.current_move_idx = 0
        self.sf_worker = None
        self.llm_worker = None

        # 상태 관리 변수
        self.last_score = "0.0"
        self.last_line = ""
        self.ai_generated_for_pos = False # 현재 포지션에서 AI 답변 생성 여부
        
        # 3초 대기를 위한 타이머 설정
        self.analysis_timer = QTimer()
        self.analysis_timer.setSingleShot(True) # 한 번만 실행
        self.analysis_timer.timeout.connect(self.request_llm_explanation)

        self.init_ui()
        self.update_display()

        
        

    def load_pgn(self, path):
        """PGN을 읽어 표준 기보 형식(1. e4 등)으로 변환하여 저장합니다."""
        with open(path, encoding='utf-8') as f:
            game = chess.pgn.read_game(f)
        
        # 시작 포지션 저장
        temp_board = game.board()
        self.positions = [temp_board.copy()]
        self.move_display_list = ["시작 포지션"]
        
        # 수순을 돌며 SAN 기보와 보드 상태 저장
        for i, move in enumerate(game.mainline_moves()):
            move_num = (i // 2) + 1
            is_white = (i % 2 == 0)
            san_move = temp_board.san(move) # 표준 기보 형식 추출
            
            display_text = f"{move_num}. {san_move}" if is_white else f"{move_num}... {san_move}"
            self.move_display_list.append(display_text)
            
            temp_board.push(move)
            self.positions.append(temp_board.copy())

    def init_ui(self):
        self.setWindowTitle("AI Chess Strategic Coach")
        self.setGeometry(100, 100, 1400, 850)
        main_layout = QHBoxLayout()

        # 1. 왼쪽: 기보 리스트 (데이터 추가 및 클릭 이벤트 연결)
        self.move_list = QListWidget()
        self.move_list.setFixedWidth(220)
        self.move_list.addItems(self.move_display_list)
        self.move_list.currentRowChanged.connect(self.on_move_clicked)
        self.move_list.setStyleSheet("font-size: 14px; background-color: #f9f9f9;")

        # 2. 중앙: 체스판
        self.board_view = QSvgWidget()
        self.board_view.setFixedSize(600, 600)

        # 3. 오른쪽: 분석 패널
        right_panel = QSplitter(Qt.Orientation.Vertical)
        
        self.engine_output = QTextEdit()
        self.engine_output.setReadOnly(True)
        self.engine_output.setStyleSheet("font-family: Consolas; background: #2b2b2b; color: #a9b7c6;")
        
        self.llm_output = QTextEdit()
        self.llm_output.setReadOnly(True)
        self.llm_output.setStyleSheet("font-family: 'Malgun Gothic'; padding: 10px;")

        eval_label = QLabel("📊 Stockfish Evaluation"); eval_label.setFixedHeight(30)
        right_panel.addWidget(eval_label)
        right_panel.addWidget(self.engine_output)
        eval_label = QLabel("🧠 Strategic Interpretation"); eval_label.setFixedHeight(30)
        right_panel.addWidget(eval_label)
        right_panel.addWidget(self.llm_output)

        main_layout.addWidget(self.move_list)
        main_layout.addWidget(self.board_view)
        main_layout.addWidget(right_panel)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # 4. 새로고침 버튼 추가
        self.refresh_btn = QPushButton("🔄 AI 분석 새로고침")
        self.refresh_btn.clicked.connect(self.manual_refresh_ai)
        self.refresh_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 5px;")
        
        # 오른쪽 패널 레이아웃에 버튼 삽입 (AI 출력창 상단에 배치 추천)
        # (기존 QSplitter나 Layout에 addWidget으로 추가해 주세요)
        # 예: self.right_layout.insertWidget(2, self.refresh_btn)

    def on_move_clicked(self, row):
        """리스트에서 기보를 클릭했을 때 해당 수순으로 이동합니다."""
        if 0 <= row < len(self.positions):
            self.current_move_idx = row
            self.update_display()

    def update_display(self):
        """현재 인덱스에 맞춰 보드, 하이라이트, 분석을 갱신합니다."""
        # 보드 이미지 업데이트
        board = self.positions[self.current_move_idx]
        svg_data = chess.svg.board(board).encode("utf-8")
        self.board_view.load(svg_data)
        
        # 리스트 하이라이트 (무한 루프 방지를 위해 시그널 일시 차단)
        self.move_list.blockSignals(True)
        self.move_list.setCurrentRow(self.current_move_idx)
        self.move_list.blockSignals(False)
        
        # 엔진 분석 및 LLM 스레드 시작 로직 (이전과 동일)
        self.start_analysis(board)

    def start_analysis(self, board):
        """수순이 바뀔 때 호출되는 핵심 분석 함수"""
        # 1. 기존 작업 중단
        if self.sf_worker and self.sf_worker.isRunning():
            self.sf_worker.stop()
            self.sf_worker.wait()
        
        self.analysis_timer.stop() # 진행 중인 3초 대기 중단
        self.ai_generated_for_pos = False # 생성 상태 초기화
        
        # 2. UI 초기화
        self.engine_output.clear()
        self.llm_output.clear()
        self.engine_output.append("🔍 엔진이 수 읽기를 시작합니다...")
        self.llm_output.setPlaceholderText("AI 코치의 분석이 진행 중입니다.")

        # 3. 스톡피시 실행
        self.sf_worker = StockfishWorker(board.fen(), self.stockfish_path)
        self.sf_worker.eval_ready.connect(self.on_engine_update)
        self.sf_worker.start()

        # 4. 3초 타이머 시작 (엔진이 깊게 생각할 시간을 줌)
        self.analysis_timer.start(3000)

    def on_engine_update(self, score, line):
        """엔진 정보 실시간 업데이트 (LLM 호출은 여기서 하지 않음)"""
        self.last_score = score
        self.last_line = line
        
        self.engine_output.clear()
        self.engine_output.append(f"<b>평가값:</b> {score}")
        self.engine_output.append(f"<b>최선 라인:</b> {line}")
    
    def request_llm_explanation(self):
        """타이머 종료 후 또는 새로고침 시 LLM에 분석 요청"""
        if not self.ai_generated_for_pos:
            # 1. 기존 워커가 있다면 처리
            if self.llm_worker and self.llm_worker.isRunning():
                # ⚡ 중요: UI와 연결된 시그널을 즉시 끊어서 섞임 방지
                try:
                    self.llm_worker.chunk_ready.disconnect()
                except TypeError:
                    pass # 이미 끊겨있는 경우 무시
                
                self.llm_worker.stop() # 내부 루프 중단 지시
                self.llm_worker.wait() # 스레드가 완전히 죽을 때까지 잠시 대기

            self.llm_output.clear()
            self.llm_output.append("🤖 AI 코치가 전략을 구성 중입니다...\n")
            
            # 2. 새 워커 생성 및 연결
            self.llm_worker = LLMWorker(self.last_line, self.last_score)
            self.llm_worker.chunk_ready.connect(self.append_llm_text)
            self.llm_worker.start()
            
            self.ai_generated_for_pos = True
            self.llm_output.clear()

    def manual_refresh_ai(self):
        """새로고침 버튼 클릭 시 강제로 다시 분석"""
        self.ai_generated_for_pos = False
        self.request_llm_explanation()

    def append_llm_text(self, text):
        """실시간으로 전달되는 텍스트 조각(chunk)을 UI에 이어 붙입니다."""
        # 커서를 맨 끝으로 이동시켜 텍스트가 자연스럽게 이어지도록 함
        cursor = self.llm_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.llm_output.setTextCursor(cursor)
    def keyPressEvent(self, event):
        # 좌우 화살표 키 처리...
        if event.key() == Qt.Key.Key_Right and self.current_move_idx < len(self.positions)-1:
            self.current_move_idx += 1
            self.update_display()
        elif event.key() == Qt.Key.Key_Left and self.current_move_idx > 0:
            self.current_move_idx -= 1
            self.update_display()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ChessAnalysisUI("example_game.pgn")
    window.show()
    sys.exit(app.exec())