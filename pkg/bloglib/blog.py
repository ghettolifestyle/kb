from os import listdir, mkdir, stat
from pathlib import Path
from boto3 import client
from boto3 import Session
from botocore.exceptions import ClientError
import markdown
from yaml import load, Loader
from shutil import move
from tempfile import TemporaryDirectory
from datetime import datetime
from re import compile, match, sub
from sys import exit as sysexit
import logging

class Blog:
    def __init__(self, config):
        # initalize boto3 using custom profile
        session = Session(profile_name='general')
        self.s3_client = session.client("s3")

        self.config = config
        self.post_dir = f"{config.base_dir}/posts"
        self.bucket = self.config.bucket

        # initial directory setup
        try:
            stat(self.config.base_dir)
        except FileNotFoundError:
            mkdir(self.config.base_dir)
            mkdir(self.post_dir)
            shutil.copytree("templates/", self.base_dir)


    def create_post(self, title=None):
        """ Creates a new Markdown-based post file in local post directory """

        if not title:
            title = input("title> ").rstrip("\n")

        alphanumeric_spaces_regexp = compile(r"[\w ]")
        formatted_title = sub(
            r"[ ]{1,}",
            "_",
            "".join(
                alphanumeric_spaces_regexp.findall(title)
            )
        )

        with open(f"{self.post_dir}/{formatted_title}.md", "a") as post_file:
            post_file.write("---\n")
            post_file.write(f"title: \"{title}\"\n")
            post_file.write(f"date: \"{int(datetime.now().timestamp())}\"\n")
            post_file.write("---\n")
            post_file.write("\n")

        logging.info(f"created post '{title}' in '{self.post_dir}'")
        

    def list_posts(self):
        # list unsynced, unpublished drafts
        print("local:")
        for index, post in enumerate(self.get_local_posts(extension=True)):
            print(f"\t[{index}] {post}")

        print()

        # list synced posts
        print("live:")
        for index, post in enumerate(self.get_synced_posts(extension=True)):
            print(f"\t[{index}] {post}")

    def prune_posts(self):
        for post in self.get_unsynced_posts():
            self.s3_client.delete_object(
                Bucket=self.bucket,
                Key=f"p/{post}.html"
            )

            logging.info(f"pruned post '{post}' from bucket")

    def sync_state(self):
        tmp_dir = TemporaryDirectory()

        self.build_posts(tmp_dir.name)
        self.build_index(tmp_dir.name)
        
        for obj in Path(tmp_dir.name).rglob("*"):
                try:
                    self.s3_client.upload_file(
                        obj,
                        self.bucket,
                        Key=f"{obj.relative_to(tmp_dir.name)}",
                        ExtraArgs={
                            "ContentType": "text/html"
                        }

                    )
                except IsADirectoryError as e:
                    pass

        self.prune_posts()

        tmp_dir.cleanup()

    def sort_posts(self, posts):
        """ Reads epoch timestamp from YAML file header and sorts posts by newest """

        if type(posts) == str:
            posts = listdir(posts)

        sorted_posts = sorted(
            posts,
            key=lambda p: datetime.fromtimestamp(
                int(
                    self.parse_post(f"{self.post_dir}/{p}")[0]["date"]
                )
            ),
            reverse=True
        )

        return sorted_posts

    def build_posts(self, dir):
        mkdir(f"{dir}/p")

        for post in self.sort_posts(self.post_dir):
            abs_markdown_path = f"{self.post_dir}/{post}"
            # e.g. "2.md" -> ["2", "md"] -> "2" + ".html"
            abs_html_path = f"{dir}/p/{post.split('.')[0]}.html"

            with open(abs_html_path, "a+") as post_file:
                # index 0 == yaml header as dict
                # index 1 == post contents in markdown
                post_metadata, post_content = self.parse_post(abs_markdown_path)
                # render content, insert html into dict for jinja to template
                post_metadata["content"] = self.render_markdown(post_content)

                post_file.write(
                    self.render_template(
                        "post.html",
                        post_metadata
                    )
                )

        logging.info(f"built post file '{abs_html_path.split('/')[-1]}' in dir '{dir}'")

    def build_index(self, dir):
        """ """

        # create a dict to pass to jinja
        vars = {}
        vars["posts"] = []

        # reads the whole file and returns the content. a little inefficient
        for post in self.sort_posts(self.get_local_posts(extension=True)):
            # leave the content, take the yaml header
            # https://www.youtube.com/watch?v=yHzh0PvMWTI
            yaml_header, _ = self.parse_post(f"{self.post_dir}/{post}")
            post_date_epoch = datetime.fromtimestamp(int(yaml_header["date"]))
            post_date_formatted = post_date_epoch.strftime("%b %d, %y")

            vars["posts"].append({
                # replace markdown file ending since we require extensions
                # to correctly reference posts during sorting operation
                "name": post.replace(".md", ""),
                "title": yaml_header["title"],
                "created_at": post_date_formatted.lower()
            })

        vars["cool_tagline"] = self.config.meta["cool_tagline"]
        vars["title"] = self.config.meta["title"]
        vars["links"] = self.config.meta["links"]

        with open(f"{dir}/index.html", "w") as index_file:
            index_file.write(
                self.render_template(
                    "index.html",
                    vars
                )
            )

        logging.info(f"built index file in dir '{dir}'")

    # friendly helper functions

    def get_synced_posts(self, extension=False):
        """ Returns list containing uploaded post files in S3 bucket """
        bucket_contents = {}
        
        try:
            bucket_contents = self.s3_client.list_objects(
                Bucket=self.bucket,
                Prefix="p/"
            )["Contents"]
        except KeyError as e:
            # if bucket is empty, ignore error
            pass

        if extension:
            synced_posts = [post["Key"].split("/")[1] for post in bucket_contents]
        else:
            synced_posts = [post["Key"].split("/")[1].split(".")[0] for post in bucket_contents]

        return synced_posts

    def get_local_posts(self, extension=False):
        """ Returns list containing post files in local post directory """
        local_posts = []

        for post in listdir(self.post_dir):
            file_name = post.split(".")[0] if not extension else post
            local_posts.append(file_name)
        return local_posts

    def get_unsynced_posts(self):
        """ Compares lists of uploaded post files in S3 bucket and local post
        directory, returns list containing elements not present in S3 bucket"""
        
        synced_posts = self.get_synced_posts()
        local_posts = self.get_local_posts()

        return list(set(synced_posts) - set(local_posts))

    def render_template(self, template, vars):
        env = self.config.jinja_env
        post_template = env.get_template(template)

        # accepts dict. how convenient
        rendered_template = post_template.render(
            vars
        )
        return rendered_template

    def render_markdown(self, markdown_content):
        return markdown.markdown(markdown_content)

    def parse_post(self, file):
        yaml_header = {}
        post_content = ""

        with open(file, "r") as post_file: 
            post_file_lines = post_file.readlines()

            triple_dash_count = 0
            yaml_header_lines = []
            yaml_header_end_line = 0

            for line in post_file_lines:
                line = line.strip()

                if triple_dash_count % 2 == 0 and triple_dash_count > 0:
                    break

                if line == "---":
                    triple_dash_count += 1
                else:
                    yaml_header_lines.append(line)

                yaml_header_end_line += 1

            yaml_header = load("\n".join(yaml_header_lines), Loader=Loader)

            # if user left line empty after yaml header, increment
            # to avoid parsing unnecessary newline
            if post_file_lines[yaml_header_end_line] == "\n":
                yaml_header_end_line += 1

            post_content = "".join(post_file_lines[yaml_header_end_line:])

        return yaml_header, post_content
