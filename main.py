#!/usr/bin/env python3
"""
main.py - Entry point for bomaylaphong

Usage:
    python main.py [--hello NAME]

Simple example CLI.
"""
import argparse


def main():
    parser = argparse.ArgumentParser(description="bomaylaphong main entrypoint")
    parser.add_argument("--hello", "-H", metavar="NAME", help="Say hello to NAME", default=None)
    args = parser.parse_args()
    if args.hello:
        print(f"Hello, {args.hello}!")
    else:
        print("Hello, world!")


if __name__ == "__main__":
    main()
