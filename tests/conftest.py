import os
import shutil
import pytest

TEST_DB = os.path.join('data', 'test_isolated_suite.db')
PROD_DB = os.path.join('data', 'noire_retail.db')

os.environ['TEST_DB_PATH'] = TEST_DB

@pytest.fixture(scope='session', autouse=True)
def setup_test_environment():
    if os.path.exists(PROD_DB):
        shutil.copyfile(PROD_DB, TEST_DB)
    
    yield

    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except Exception:
            pass
