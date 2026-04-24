import zipfile
from pathlib import Path

def prepare_stockfish():
    zip_path = Path("stockfish.zip")
    # 압축을 풀어서 생길 폴더나 실행 파일 이름을 지정하세요.
    # 여기서는 'stockfish'라는 폴더가 생기는 것을 기준으로 작성했습니다.
    extract_path = Path("stockfish")

    # 1. 압축 파일 자체가 존재하는지 확인
    if not zip_path.exists():
        print(f"오류: {zip_path} 파일이 현재 경로에 없습니다.")
        return

    # 2. 이미 압축이 풀려 있는지 확인 (폴더 존재 여부)
    if not extract_path.exists():
        print("Stockfish 압축을 푸는 중...")
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(".")  # 현재 경로에 압축 해제
            print("압축 해제 완료!")
        except zipfile.BadZipFile:
            print("오류: 압축 파일이 손상되었습니다.")
    else:
        # 이미 존재하면 아무것도 하지 않음
        pass

# 프로그램 시작 시 호출
prepare_stockfish()