docker build -t gitlab-mr-reviewer .
docker run --name gitlab-mr-reviewer-test -p 5000:5000 -d gitlab-mr-reviewer