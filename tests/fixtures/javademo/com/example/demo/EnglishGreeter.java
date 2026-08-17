package com.example.demo;

public class EnglishGreeter implements Greeter {
    private final Formatter formatter;

    public EnglishGreeter() {
        this.formatter = new Formatter();
    }

    @Override
    public String greet(String name) {
        return formatter.format(name);
    }
}
