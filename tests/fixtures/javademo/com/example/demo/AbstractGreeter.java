package com.example.demo;

public abstract class AbstractGreeter implements Greeter {
    protected int callCount;

    protected void trackCall() {
        callCount++;
    }
}
