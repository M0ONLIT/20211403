import os
import platform
import urllib.request
import zipfile
from pathlib import Path

def download_stockfish():
    # 1. 운영체제 및 아키텍처 확인
    os_name = platform.system() # 'Windows', 'Linux', 'Darwin' (Mac)
    
    # 깃허브 최신 릴리스 주소 (예시: Windows용 AVX2 버전)
    # 실제 구현 시에는 운영체제별로 URL을 분기 처리하는 것이 좋습니다.
    url = "https://github.com/official-stockfish/Stockfish/releases/latest/download/stockfish-windows-x86-64-avx2.zip"
    
    # 저장할 파일 이름과 압축 해제 경로
    zip_name = "stockfish.zip"
    extract_folder = Path("stockfish")

    # 2. 이미 설치되어 있는지 확인
    if extract_folder.exists():
        print("Stockfish가 이미 설치되어 있습니다.")
        return

    # 3. 다운로드 시작
    try:
        print(f"Stockfish 다운로드 중... ({url})")
        urllib.request.urlretrieve(url, zip_name)
        print("다운로드 완료!")

        # 4. 압축 해제
        print("압축 해제 중...")
        with zipfile.ZipFile(zip_name, 'r') as zip_ref:
            zip_ref.extractall(extract_folder)
        
        # 5. 임시 ZIP 파일 삭제
        os.remove(zip_name)
        print(f"설치 완료! 경로: {extract_folder}")

        # 6. 리눅스/맥의 경우 실행 권한 부여 (chmod +x)
        if os_name != "Windows":
            # 실제 실행 파일 경로를 찾아 권한 부여
            for root, dirs, files in os.walk(extract_folder):
                for f in files:
                    os.chmod(os.path.join(root, f), 0o755)

    except Exception as e:
        print(f"오류 발생: {e}")

if __name__ == "__main__":
    download_stockfish()