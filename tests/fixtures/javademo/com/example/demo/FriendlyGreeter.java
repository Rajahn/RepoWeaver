package com.example.demo;

public class FriendlyGreeter extends AbstractGreeter {
    private final Formatter formatter = new Formatter();

    @Override
    public String greet(String name) {
        trackCall();
        return formatter.format(name) + "!";
    }
}
